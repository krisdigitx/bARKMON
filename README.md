# Barkmon

A multi-camera barking monitor for the Proxmox Kubernetes home lab. It captures the Tapo C211's RTSP audio, detects barking with TensorFlow Lite YAMNet, and provides a dashboard with playback, review, coverage, hourly activity, and CSV/PDF reports.

## Architecture

One pod runs two containers: FastAPI serves the dashboard/API; a worker maintains one FFmpeg connection per configured camera and analyses their completed recordings sequentially with one shared YAMNet interpreter. Both share the existing `kangal-monitor/barkmon-data` 20 Gi PVC. Local PV affinity places the pod on `k8s-wk-01`; `Recreate` deployment strategy prevents overlapping recorder pods during rollout. Both cameras use the same Vault-synced `tapo-credentials` Secret.

SQLite WAL stores segment metadata, detections and review decisions on the same local disk. This replaces the earlier proposed PostgreSQL/React services to keep the first deployment small. The dashboard is dependency-free HTML/CSS/JavaScript served by FastAPI. No cloud audio processing is used.

FFmpeg converts the camera's 8 kHz mono A-law audio into 16 kHz mono PCM and atomically publishes 60-second WAV segments. Resampling does not restore missing frequencies. Interrupted streams save the partial segment and retry; stale capture status and gaps appear in coverage. Completed but unprocessed segments survive worker restarts. New recordings can take about a minute to appear.

YAMNet evaluates overlapping 0.975-second windows, every 0.48 seconds. Bark, yip, howl and bow-wow classes count toward detection. Adjacent positive hops form an **estimated barking episode**. These are not exact individual bark counts, dog identity predictions, calibrated decibels, or validated nuisance measurements. Confirm/reject detections after listening; rejected detections are excluded from summary totals. Review does not retrain the model. Silence/model smoke tests do not establish accuracy on this camera.

## Deploy through GitHub Actions and ArgoCD

1. The existing home-lab Terraform/Ansible workflow must provision and mount the disk and create the PV/PVC. This storage has already passed the Kubernetes read/write test. Barkmon's ArgoCD application does not own or prune the storage resources.
2. Create `tapo-credentials` in `kangal-monitor` with keys `username` and `password` using the camera account. Use rotated credentials, not the credentials previously shared in chat. For example, on the bastion with Bash:

   ```bash
   read -r -p 'Camera username: ' RTSP_USER
   read -r -s -p 'Camera password: ' RTSP_PASS; echo
   kubectl -n kangal-monitor create secret generic tapo-credentials \
     --from-literal=username="$RTSP_USER" \
     --from-literal=password="$RTSP_PASS"
   unset RTSP_USER RTSP_PASS
   ```

   Do not commit camera credentials. The API container does not receive them. FFmpeg needs the authenticated URL as a process argument inside the worker; avoid sharing process dumps. Application logs suppress FFmpeg stderr and URLs.
3. Push the barkmon source to `krisdigitx/bARKMON` on `main`. The **Test and publish barkmon** workflow runs tests including real YAMNet inference, builds a Linux amd64 image for Proxmox, and publishes `ghcr.io/krisdigitx/barkmon`. It then commits the image digest to `deploy/kustomization.yaml`, giving ArgoCD an immutable image change to roll out. The workflow needs repository Contents write and Packages write permissions; branch protection must permit the bot's deployment commit. If direct bot commits are prohibited, update the digest through your normal PR workflow.
4. Ensure the cluster can pull the GHCR package. For a private package, configure an existing read-only registry credential as `imagePullSecrets` on the deployment's pod spec. Alternatively, make this application image package public if appropriate; the image contains no camera credentials or recordings.
5. Push `home-lab/argocd/applications/barkmon.yaml` to home-lab `main`. The existing root ArgoCD application discovers it and syncs `bARKMON/deploy` into `kangal-monitor`. ArgoCD needs repository access if the source repository is private.

Check from the bastion:

```bash
kubectl -n kangal-monitor rollout status deployment/barkmon --timeout=180s
kubectl -n kangal-monitor get pods,pvc
kubectl -n kangal-monitor logs deployment/barkmon -c worker --tail=50
kubectl -n kangal-monitor port-forward service/barkmon 8080:80
```

For direct LAN access, the Service uses MetalLB's existing `home-lab-pool` (`192.168.1.240–192.168.1.250`). The Service requests `192.168.1.249`. The previous automatically allocated `.247` was answered by another LAN device (`Server: SHIP 2.0`), so it must not be used for barkmon. Keep `.249` reserved outside the router DHCP allocation range. After ArgoCD sync:

```bash
kubectl -n kangal-monitor get service barkmon -w
```

Open `http://<EXTERNAL-IP>` from a device on the LAN. If the address remains pending, run `kubectl -n kangal-monitor describe service barkmon` and check the pool has available addresses. The existing port-forward command is an alternative: open `http://localhost:8080` on the machine running port-forward. If forwarding from your laptop through the bastion, use a second terminal on the laptop:

```bash
ssh -L 8080:127.0.0.1:8080 ubuntu@192.168.1.210
```

The Service exposes HTTP port 80 on the LAN through MetalLB. There is no application login in this initial version; LAN clients that can reach the address can access recordings and review controls. Use this on the trusted home network; add an authenticated proxy for broader access. A missing camera Secret prevents the worker container from starting; a disconnected camera is retried without failing the API. `/api/status` distinguishes worker and camera health.

## Cameras and connection diagnostics

The dashboard's camera selector switches status, recordings, detections, reviews, reports and playback together. Both cameras record independently in the background; selecting a camera does not stop the other. Default home-lab addresses are Camera 1 `192.168.1.111` and Camera 2 `192.168.1.200`, both using `/stream1` and the same camera account from Vault. Clear `CAMERA2_HOST` to run only the first camera.

Camera one's existing files/database stay at `/data/recordings` and `/data/barkmon.sqlite3`; camera two uses `/data/cameras/camera2/recordings` and `/data/cameras/camera2/barkmon.sqlite3`. No history migration is required. Event IDs are scoped to a camera: APIs accept `?camera=camera1` or `?camera=camera2`. `/api/cameras` lists the configured cameras and their status.

**Check / reconnect** requests a fresh connection for the selected camera and briefly interrupts its capture. It does not restart the other camera, change settings, or read credentials in the browser. The worker tests TCP first, then opens RTSP. Its status panel reports TCP reachability, authentication failure, missing stream, unsupported recorder option or audio timeout. Raw FFmpeg messages and credential-bearing URLs are not returned or logged. A worker restart is not required for the button; the worker processes the request automatically. Network or camera failures are retried every five seconds after the failed attempt finishes.

Both camera addresses need the targeted NAT workaround in this home lab. Reapply home-lab's separate `ansible/barkmon-camera-nat.yml` playbook after pulling its updated two-address configuration. If a camera is unreachable from the worker host itself, check its power, Wi-Fi and current IP in the Tapo app; NAT cannot restore an offline/moved camera.

The configured 16 GiB audio budget is **total**, split equally between configured cameras (8 GiB each with two cameras). The volume free-space reserve is shared. At two continuous PCM streams, this permits roughly three days of audio before the space cap, despite the seven-day age limit. Reports and retention are camera-specific; this version does not combine episodes across cameras or identify which dog barked.

## Recording and processing statistics

The dashboard's **From camera to clarity** panel refreshes every 15 seconds and covers all retained recordings for the selected camera, independently of the selected reporting period. It shows indexed WAV file count and audio hours, audio bytes against the configured budget, volume free space, database size, processed audio percentage, processed/pending/failed file counts, oldest queued audio age, first/latest retained recording times, review totals, recorder/detector state, and the latest file's analysis time. Processing timing becomes available after the updated worker analyses its next file.

The in-flight recording buffer is not counted until a file is saved. Processed percentage uses audio duration, so a short interrupted segment has proportionately less weight than a full minute. These inventory statistics are available from `/api/pipeline`. The expandable explanation describes capture, file storage, detection, and retention.

## Configuration

Edit `deploy/config.env`; Kustomize generates a versioned ConfigMap so changes trigger rollout automatically.

| Setting | Default | Purpose |
| --- | --- | --- |
| `CAMERA_HOST` | `192.168.1.111` | Private RTSP camera IP or hostname, port 554 |
| `CAMERA_PATH` | `/stream1` | First camera RTSP path |
| `CAMERA2_HOST` | `192.168.1.200` in deployment | Second camera; empty disables it |
| `CAMERA2_PATH` | `/stream1` | Second camera RTSP path |
| `BARK_THRESHOLD` | `0.35` | Initial YAMNet score threshold; validate against real clips |
| `REPORT_TIMEZONE` | `Europe/London` | Calendar dates and night boundaries, with DST |
| `NIGHT_START_HOUR` / `NIGHT_END_HOUR` | `22` / `7` | Night begins on the selected starting date |
| `RETENTION_DAYS` | `7` | Maximum recording and event retention |
| `MAX_STORAGE_GIB` | `16` | Audio budget; oldest recordings/events deleted first |
| `MIN_FREE_GIB` | `1` | Minimum disk reserve; stop recording if it cannot be recovered |

Retention runs at reconnect and each completed segment. At 16 kHz PCM, continuous audio uses about 2.6 GiB/day, so the total 16 GiB budget can shorten retention below seven days (approximately three days for two cameras). Metadata and audio are removed together; reports cover **retained data only**. Download reports you need to preserve. Recording and analysis coverage measure the elapsed portion of the selected period, not future hours; missing, expired or unanalysed audio cannot substantiate silence. Night windows account for daylight-saving changes. Whole segments back playback, with the player seeking to the detection.

The initial resource requests total 384 MiB RAM and 350m CPU; limits total 1152 MiB RAM and 3 CPU. Measure actual load and model accuracy on the worker. The application is intentionally one replica; this local volume and SQLite are not an HA design. Back up the mounted data, using SQLite's backup API or a stopped application rather than copying only the live database file without its WAL.

## Development and validation

API/dashboard only (macOS or Linux, Python 3.11–3.13):

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
ruff check .
DATA_DIR=./data uvicorn barkmon.api:app --reload
```

Full worker runtime uses Linux Python 3.11 and `tflite-runtime==2.14.0` with NumPy 1.x:

```bash
docker build -t barkmon:local .
```

The Docker build downloads the official YAMNet model and verifies a pinned SHA-256. CI runs the same model against silence and checks model dimensions and labels, alongside functional tests for capture/disconnection, restart recovery, retention, coverage, DST, review, playback and reports. Use actual Kangal recordings to assess false positives and missed barks before relying on report totals.

## Model attribution

YAMNet is provided by TensorFlow under Apache-2.0. Its bundled metadata includes the class labels. Source and model provenance:

- [Official YAMNet documentation](https://www.tensorflow.org/hub/tutorials/yamnet)
- [TensorFlow Lite official sample and model download](https://github.com/tensorflow/examples/tree/master/lite/examples/audio_classification/raspberry_pi)
- [YAMNet source and license](https://github.com/tensorflow/models/tree/master/research/audioset/yamnet)

The checksum and exact download URL are in `scripts/download_model.py`.

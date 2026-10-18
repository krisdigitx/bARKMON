# Bark monitor — recovered project context

Retrieved on 17 October 2026 from the ChatGPT **Home Cluster** project, conversation **Legal Advice On Barking** (`6ac7d811-0398-83eb-830e-3556eeab00d2`). The accessible conversation contains five recent exchanges; this brief does not claim to cover unavailable earlier history or project files.

## Goal and requirements discussed

Build a home-lab application, previously called **Kangal Bark Monitor**, to record and analyse the Kangal's barking automatically, without leaving a Mac running. The prior assistant referred to a Labrador that does not bark; treat this as context to confirm, not reliable dog identification by the model.

Dashboard requirements discussed:

- Estimated individual bark count and total barking duration.
- Timestamped timeline, longest barking episode, nightly and weekly statistics.
- Playback of recorded barking clips.
- Recording coverage and explicit gaps in capture.
- Manual review and correction of automated detections.
- Exportable nightly PDF report, described as a council report.

Counts and classification are estimates requiring validation. No validated detection accuracy or sound-pressure measurement capability has been established.

## Confirmed by the user's tests

- Existing cameras: TP-Link Tapo C211 and C510W; C211 selected as the initial source.
- C211 address: `192.168.1.111`, RTSP port `554`, path `/stream1`.
- Mac TCP connectivity and authenticated ffprobe succeeded.
- Audio: PCM A-law, 8,000 Hz, mono, 64 kb/s.
- Video present: H.264, 2304 × 1296; the proposed recorder extracts audio only.
- User confirmed the Kubernetes cluster and camera are on the same network.

Camera credentials appeared in the original conversation. They are deliberately excluded here. The prior conversation requested password rotation; completion is unknown.

## Proposed architecture (not yet implemented or validated)

Tapo RTSP → single FFmpeg recorder → timestamped 60-second audio segments on persistent storage → bark detection worker → PostgreSQL and bark clips → FastAPI API and React dashboard.

- Initial classifier proposal: YAMNet, with audio segmentation to estimate bark counts and durations. Model selection and accuracy remain to be validated against this camera's 8 kHz source.
- Proposed processing format: 16 kHz mono PCM WAV; resampling cannot restore frequencies absent from the source.
- Reconnect after stream interruptions; record coverage gaps; process completed segments.
- Avoid concurrent recorder replicas opening multiple camera streams.
- Use a persistent volume initially; S3/MinIO was deferred.
- Proposed Kubernetes namespace: `kangal-monitor`; camera Secret: `tapo-credentials`.
- Deploy through ArgoCD; expose operational metrics for Prometheus/Grafana.
- Prior rough RAM estimates: recorder 128 MiB, detector 512 MiB–1 GiB, API 256 MiB, dashboard 128 MiB, PostgreSQL 256 MiB. These are untested estimates.

## Last pending step

Test TCP access from an actual Kubernetes pod to `192.168.1.111:554`. Same-network placement and a successful Mac test do not establish pod connectivity. No pod test result, successful audio playback, namespace creation, Secret creation, or deployed recorder was present in the retrieved exchanges.

## Local repository context

`/Users/krisdigitx/Source/home-lab` describes a Proxmox cluster provisioned through Terraform and Ansible, with kubeadm, containerd and Calico. Its README lists target/example addresses: bastion `.200`, control plane `.120`, workers `.121`–`.123` on `192.168.1.0/24`. These are documentation values, not a live-state check.

The repository has ArgoCD configuration and an existing Cloudflare Tunnel/Vault Secrets Operator integration for JSS Xray. Reuse patterns after inspecting their configuration; no bark-monitor deployment was found by a repository text search. Prometheus/Grafana were proposed in the chat but their live availability has not been checked.

## Decisions still needed during implementation

Validate pod-to-camera access, persistent storage availability/capacity, retention and deletion policy, night-time reporting window/timezone, classifier thresholds and manual review workflow, authentication/access method, deployment hostname, and reporting format. Confirm the deployment resources against live cluster capacity.

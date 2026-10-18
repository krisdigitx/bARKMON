"""Independent camera recording with a shared, sequential detector."""

import logging
import os
import select
import shutil
import signal
import socket
import errno
import subprocess
import threading
import time
import uuid
import wave
from urllib.parse import quote

import numpy as np

from barkmon.config import Settings, camera_settings
from barkmon.detection import RATE, Yamnet
from barkmon.store import Store

log = logging.getLogger("barkmon")


def save_audio(settings, store, pcm, start):
    pcm = pcm[: len(pcm) // 2 * 2]
    if not pcm:
        return
    name = f"{int(start * 1000)}-{uuid.uuid4().hex}.wav"
    temporary = settings.recordings / (name + ".tmp")
    with wave.open(str(temporary), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes(pcm)
    destination = settings.recordings / name
    temporary.replace(destination)
    store.add_segment(name, start, len(pcm) / (RATE * 2), destination.stat().st_size)


def recover(settings, store):
    # Recover files atomically published before a crash but not yet indexed.
    for path in settings.recordings.glob("*.wav"):
        with store.connect() as db:
            known = db.execute(
                "SELECT 1 FROM segments WHERE id=?", (path.name,)
            ).fetchone()
        if not known:
            with wave.open(str(path)) as stream:
                store.add_segment(
                    path.name,
                    int(path.name.split("-")[0]) / 1000,
                    stream.getnframes() / stream.getframerate(),
                    path.stat().st_size,
                )
    for path in settings.recordings.glob("*.tmp"):
        path.unlink(missing_ok=True)


def cleanup(settings, store, now=None):
    now = time.time() if now is None else now
    with store.connect() as db:
        segments = db.execute("SELECT * FROM segments ORDER BY start").fetchall()
    total = sum(r["bytes"] for r in segments)
    free = shutil.disk_usage(settings.data_dir).free
    deleted = 0
    for row in segments:
        expired = row["start"] + row["duration"] < now - settings.retention_days * 86400
        if (
            not expired
            and total <= settings.max_storage_bytes
            and free >= settings.min_free_bytes
        ):
            break
        # Prefer releasing space even if detection has fallen behind. Report this.
        with store.connect() as db:
            db.execute("DELETE FROM segments WHERE id=?", (row["id"],))
        (settings.recordings / row["id"]).unlink(missing_ok=True)
        total -= row["bytes"]
        free += row["bytes"]
        deleted += 1
    if deleted:
        store.status(
            "retention", f"Removed {deleted} segments; reports cover retained data only"
        )
    if free < settings.min_free_bytes:
        raise OSError("Storage reserve exhausted")


def connection_error(message):
    """Return safe fixed diagnostics; never retain raw FFmpeg output or URLs."""
    message = message.lower()
    if "401" in message or "unauthorized" in message:
        return "RTSP authentication failed (401); check the camera account in Vault"
    if "403" in message or "forbidden" in message:
        return "RTSP access denied (403); check camera account permissions"
    if "404" in message or "stream not found" in message:
        return "RTSP stream not found; check the configured stream path"
    if "option" in message and ("not found" in message or "unrecognized" in message):
        return "Recorder option unsupported by this FFmpeg version"
    if "connection refused" in message:
        return "Camera refused RTSP; check that RTSP is enabled"
    if "timed out" in message or "timeout" in message:
        return "RTSP timed out; check camera connectivity and stream availability"
    if "no route to host" in message or "network is unreachable" in message:
        return "Camera unreachable; check camera IP, LAN routing and NAT"
    if "does not contain any stream" in message or "matches no streams" in message:
        return "Camera stream has no usable audio track"
    return None


def monitor_errors(pipe, diagnostics):
    for line in iter(pipe.readline, b""):
        reason = connection_error(line.decode(errors="replace"))
        if reason:
            # Authentication errors take precedence over generic end-of-stream errors.
            if not diagnostics.get("reason", "").startswith("RTSP authentication"):
                diagnostics["reason"] = reason


def record(settings, store, stop):
    user = os.environ["RTSP_USERNAME"]
    password = os.environ["RTSP_PASSWORD"]
    host = settings.camera_host
    path = settings.camera_path
    url = f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{host}:554{path}"
    while not stop.is_set():
        process = None
        error_reader = None
        diagnostics = {}
        last_request = store.statuses().get("reconnect_request", {}).get("updated", 0)
        pcm = bytearray()
        start = None
        try:
            cleanup(settings, store)
            store.status("recorder", "connecting")
            store.status("connection_detail", "Checking camera TCP port 554")
            try:
                with socket.create_connection((host, 554), timeout=5):
                    pass
            except OSError as error:
                reason = (
                    "TCP connection timed out; check camera power/IP and targeted NAT"
                )
                if error.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
                    reason = (
                        "Camera unreachable on the network; check its IP and routing"
                    )
                elif error.errno == errno.ECONNREFUSED:
                    reason = "Camera refused TCP port 554; check that RTSP is enabled"
                diagnostics["reason"] = reason
                raise
            store.status(
                "connection_detail", "TCP reachable; opening authenticated RTSP audio"
            )
            # Never log argv or raw FFmpeg stderr: both can contain credentials.
            process = subprocess.Popen(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-rtsp_transport",
                    "tcp",
                    "-timeout",
                    "15000000",
                    "-i",
                    url,
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    str(RATE),
                    "-f",
                    "s16le",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            error_reader = threading.Thread(
                target=monitor_errors, args=(process.stderr, diagnostics), daemon=True
            )
            error_reader.start()
            last_request_check = 0
            last_data = time.monotonic()
            last_status = 0
            target = settings.segment_seconds * RATE * 2
            while not stop.is_set():
                if time.monotonic() - last_request_check > 1:
                    last_request_check = time.monotonic()
                    request = (
                        store.statuses().get("reconnect_request", {}).get("updated", 0)
                    )
                    if request > last_request:
                        store.status(
                            "connection_detail",
                            "Reconnect requested; checking again shortly",
                        )
                        break
                if not select.select([process.stdout], [], [], 1)[0]:
                    if time.monotonic() - last_data > 20:
                        raise TimeoutError("No audio received")
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise ConnectionError("Camera stream closed")
                arrival = time.monotonic()
                # Split on stalls so a capture gap is not reported as coverage.
                if arrival - last_data > 3:
                    if pcm:
                        save_audio(settings, store, pcm, start)
                        pcm.clear()
                    start = None
                last_data = arrival
                if start is None:
                    start = time.time() - len(chunk) / (RATE * 2)
                pcm.extend(chunk)
                if last_data - last_status > 5:
                    store.status("recorder", "recording")
                    store.status("connection_detail", "Receiving audio over RTSP")
                    last_status = last_data
                while len(pcm) >= target:
                    save_audio(settings, store, bytes(pcm[:target]), start)
                    del pcm[:target]
                    start += settings.segment_seconds
                    cleanup(settings, store)
        except Exception as error:
            # Only log exception type, never exception text that may contain URLs.
            if error_reader:
                error_reader.join(timeout=0.1)
            detail = (
                diagnostics.get("reason")
                or "RTSP audio stopped before data arrived; check stream availability"
            )
            log.warning("Camera %s: %s", settings.camera_id, detail)
            store.status("connection_detail", detail)
            store.status("recorder", f"disconnected ({type(error).__name__}); retrying")
        finally:
            if process:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stdout.close()
                if error_reader:
                    error_reader.join(timeout=2)
                process.stderr.close()
            if pcm and start is not None:
                try:
                    save_audio(settings, store, pcm, start)
                except OSError:
                    store.status("recorder", "storage write failed")
        stop.wait(5)


def run():
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    stores = [Store(camera) for camera in camera_settings(settings)]
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    for store in stores:
        recover(store.settings, store)
    model = Yamnet(settings.model_path, settings.threshold)
    recorders = [
        threading.Thread(target=record, args=(store.settings, store, stop), daemon=True)
        for store in stores
    ]
    for recorder in recorders:
        recorder.start()
    try:
        while not stop.is_set():
            for store, recorder in zip(stores, recorders):
                store.status("worker", "running")
                if not recorder.is_alive():
                    raise RuntimeError("Recorder thread stopped")
            # Rotate the scan so one camera cannot starve the other detector queue.
            stores.append(stores.pop(0))
            recorders.append(recorders.pop(0))
            row = None
            for store in stores:
                with store.connect() as db:
                    row = db.execute(
                        "SELECT * FROM segments WHERE state='pending' ORDER BY start LIMIT 1"
                    ).fetchone()
                if row:
                    break
            if row:
                try:
                    with wave.open(
                        str(store.settings.recordings / row["id"])
                    ) as stream:
                        audio = (
                            np.frombuffer(
                                stream.readframes(stream.getnframes()), dtype="<i2"
                            ).astype(np.float32)
                            / 32768
                        )
                    processing_started = time.monotonic()
                    events = model.detect(audio, row["start"])
                    store.complete(row["id"], events)
                    store.status(
                        "last_processing_seconds",
                        f"{time.monotonic() - processing_started:.3f}",
                    )
                    store.status("detector", "running")
                except Exception as error:
                    log.warning("Segment processing failed: %s", type(error).__name__)
                    with store.connect() as db:
                        db.execute(
                            "UPDATE segments SET attempts=attempts+1, state=CASE WHEN attempts>=2 THEN 'failed' ELSE 'pending' END WHERE id=?",
                            (row["id"],),
                        )
                    store.status("detector", "segment processing failed")
                    stop.wait(1)
            else:
                stop.wait(2)
    finally:
        stop.set()
        for recorder in recorders:
            recorder.join(timeout=30)
        for store in stores:
            store.status("worker", "stopped")


if __name__ == "__main__":
    run()

"""Single-camera recording and durable, restartable segment processing."""

import logging
import os
import select
import shutil
import signal
import subprocess
import threading
import time
import uuid
import wave
from urllib.parse import quote

import numpy as np

from barkmon.config import Settings
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


def record(settings, store, stop):
    user = os.environ["RTSP_USERNAME"]
    password = os.environ["RTSP_PASSWORD"]
    host = os.getenv("CAMERA_HOST", "192.168.1.111")
    path = os.getenv("CAMERA_PATH", "/stream1")
    url = f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{host}:554{path}"
    while not stop.is_set():
        process = None
        pcm = bytearray()
        start = None
        try:
            cleanup(settings, store)
            store.status("recorder", "connecting")
            # Never log argv or FFmpeg stderr: both can contain camera credentials.
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
                stderr=subprocess.DEVNULL,
            )
            last_data = time.monotonic()
            last_status = 0
            target = settings.segment_seconds * RATE * 2
            while not stop.is_set():
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
                    last_status = last_data
                while len(pcm) >= target:
                    save_audio(settings, store, bytes(pcm[:target]), start)
                    del pcm[:target]
                    start += settings.segment_seconds
                    cleanup(settings, store)
        except Exception as error:
            # Only log exception type, never exception text that may contain URLs.
            log.warning("Recorder interrupted: %s", type(error).__name__)
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
            if pcm and start is not None:
                try:
                    save_audio(settings, store, pcm, start)
                except OSError:
                    store.status("recorder", "storage write failed")
        stop.wait(5)


def run():
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    store = Store(settings)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    recover(settings, store)
    model = Yamnet(settings.model_path, settings.threshold)
    recorder = threading.Thread(
        target=record, args=(settings, store, stop), daemon=True
    )
    recorder.start()
    try:
        while not stop.is_set():
            store.status("worker", "running")
            if not recorder.is_alive():
                raise RuntimeError("Recorder thread stopped")
            with store.connect() as db:
                row = db.execute(
                    "SELECT * FROM segments WHERE state='pending' ORDER BY start LIMIT 1"
                ).fetchone()
            if row:
                try:
                    with wave.open(str(settings.recordings / row["id"])) as stream:
                        audio = (
                            np.frombuffer(
                                stream.readframes(stream.getnframes()), dtype="<i2"
                            ).astype(np.float32)
                            / 32768
                        )
                    store.complete(row["id"], model.detect(audio, row["start"]))
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
        recorder.join(timeout=30)
        store.status("worker", "stopped")


if __name__ == "__main__":
    run()

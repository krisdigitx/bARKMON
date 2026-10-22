"""Rolling RTSP video buffers and browser-compatible per-detection evidence."""

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import quote


def root_dir(settings):
    return (
        settings.data_dir.parents[1]
        if settings.camera_id == "camera2"
        else settings.data_dir
    )


def retention_days(settings):
    try:
        return int(
            json.loads((root_dir(settings) / "retention.json").read_text())["days"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return settings.retention_days


def set_retention(settings, days):
    root = root_dir(settings)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f"retention-{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps({"days": days}))
    temporary.replace(root / "retention.json")


def video_dir(settings):
    directory = settings.data_dir / "video"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def capture_video(settings, store, stop):
    buffer = video_dir(settings) / "buffer"
    buffer.mkdir(exist_ok=True)
    url = (
        f"rtsp://{quote(os.environ['RTSP_USERNAME'], safe='')}:"
        f"{quote(os.environ['RTSP_PASSWORD'], safe='')}@{settings.camera_host}:554{settings.video_path}"
    )
    while not stop.is_set():
        process = None
        try:
            # Short, independently playable files; only completed files are read.
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
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-c:a",
                    "aac",
                    "-c:v",
                    "copy",
                    "-f",
                    "segment",
                    "-segment_time",
                    "10",
                    "-reset_timestamps",
                    "1",
                    "-strftime",
                    "1",
                    str(buffer / "%s.mkv"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            while not stop.wait(2):
                if process.poll() is not None:
                    break
                if shutil.disk_usage(settings.data_dir).free < settings.min_free_bytes:
                    store.status("video_capture", "Storage reserve exhausted; retrying")
                    break
                store.status("video_capture", "buffering")
        except OSError:
            pass
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            store.status("video_capture", "disconnected; retrying")
        stop.wait(5)


def duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )
    return float(result.stdout)


def make_clip(source, event, target):
    """Return False while waiting for the segment covering the event to close."""
    files = sorted(
        (video_dir(source) / "buffer").glob("*.mkv"), key=lambda p: int(p.stem)
    )
    # The last file may still be open. Never publish partial media.
    ready = files[:-1]
    begin, end = event["start"] - 2, event["end"] + 2
    selected = []
    for path in ready:
        stamp = int(path.stem)
        if stamp > end or stamp + 120 < begin:
            continue
        length = duration(path)
        if stamp + length > begin and stamp < end:
            selected.append((path, stamp, length))
    if (
        not selected
        or selected[0][1] > event["start"]
        or selected[-1][1] + selected[-1][2] < end
    ):
        return False
    if any(b[1] - (a[1] + a[2]) > 2 for a, b in zip(selected, selected[1:])):
        return False
    playlist = target.with_suffix(".txt")
    temporary = target.with_suffix(".tmp.mp4")
    try:
        playlist.write_text("".join(f"file '{p.as_posix()}'\n" for p, _, _ in selected))
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(playlist),
                "-ss",
                str(max(0, begin - selected[0][1])),
                "-t",
                str(end - max(begin, selected[0][1])),
                "-c:a",
                "aac",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-threads",
                "2",
                "-crf",
                "25",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=True,
        )
        temporary.replace(target)
        os.utime(target, (event["end"], event["end"]))
        return True
    finally:
        playlist.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def cleanup_video(settings, now=None):
    now = time.time() if now is None else now
    directory = video_dir(settings)
    for temporary in list(directory.glob("*.tmp.mp4")) + list(directory.glob("*.txt")):
        if temporary.stat().st_mtime < now - 300:
            temporary.unlink(missing_ok=True)
    buffer = sorted((directory / "buffer").glob("*.mkv"), key=lambda p: int(p.stem))
    for path in buffer[:-1]:
        if int(path.stem) < now - settings.video_buffer_seconds:
            path.unlink(missing_ok=True)
    clips = sorted(
        (p for p in directory.glob("*.mp4") if not p.name.endswith(".tmp.mp4")),
        key=lambda p: p.stat().st_mtime,
    )
    for path in clips:
        if path.stat().st_mtime < now - retention_days(settings) * 86400:
            path.unlink(missing_ok=True)
    # Include video and audio in the existing per-camera shared disk budget.
    media = (
        list(settings.recordings.glob("*.wav"))
        + list(directory.rglob("*.mkv"))
        + list(directory.glob("*.mp4"))
    )
    total = sum(p.stat().st_size for p in media if p.exists())
    free = shutil.disk_usage(settings.data_dir).free
    for path in clips:
        if total <= settings.max_storage_bytes and free >= settings.min_free_bytes:
            break
        if path.exists():
            size = path.stat().st_size
            path.unlink()
            total -= size
            free += size


def video_loop(stores, stop):
    while not stop.is_set():
        for store in stores:
            try:
                cleanup_video(store.settings)
                with store.connect() as db:
                    events = db.execute(
                        "SELECT * FROM events WHERE start>? ORDER BY start",
                        (time.time() - store.settings.video_buffer_seconds,),
                    ).fetchall()
                for event in events:
                    for source in stores:
                        if stop.is_set():
                            return
                        name = clip_name(event, source.settings.camera_id)
                        target = video_dir(store.settings) / name
                        if not target.exists():
                            try:
                                make_clip(source.settings, event, target)
                            except (OSError, ValueError, subprocess.SubprocessError):
                                store.status(
                                    "video_processing",
                                    "A source clip is unavailable; other cameras continue",
                                )
            except (OSError, ValueError, subprocess.SubprocessError):
                store.status(
                    "video_processing",
                    "Video unavailable or processing failed; retrying",
                )
        stop.wait(5)


def clip_name(event, source_id):
    return f"{event['id']}-{Path(event['segment_id']).stem}-{source_id}.mp4"

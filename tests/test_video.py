import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from barkmon.api import create_app
from barkmon.config import Settings, camera_settings
from barkmon.detection import Event
from barkmon.store import Store
from barkmon.video import (
    cleanup_video,
    clip_name,
    duration,
    make_clip,
    retention_days,
    set_retention,
    video_dir,
)


def test_retention_policy_shared_and_persistent(tmp_path):
    settings = Settings(data_dir=tmp_path, camera2_host="192.168.1.206")
    with TestClient(create_app(settings)) as client:
        assert (
            client.put(
                "/api/settings/retention",
                json={"days": 30},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.put("/api/settings/retention", json={"days": 0}).status_code == 422
        )
        assert (
            client.put("/api/settings/retention", json={"days": True}).status_code
            == 422
        )
        assert client.put("/api/settings/retention", json={"days": 30}).json() == {
            "days": 30
        }
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/settings/retention").json() == {"days": 30}
    assert all(retention_days(c) == 30 for c in camera_settings(settings))
    for camera in camera_settings(settings):
        old = video_dir(camera) / "old.mp4"
        old.write_bytes(b"old")
        recent = video_dir(camera) / "recent.mp4"
        recent.write_bytes(b"new")
        os.utime(old, (100, 100))
        os.utime(recent, (31 * 86400, 31 * 86400))
        cleanup_video(camera, 32 * 86400)
        assert not old.exists() and recent.exists()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_video_clip_encoding_playback_and_range(tmp_path):
    settings = Settings(
        data_dir=tmp_path, camera2_host="192.168.1.206", min_free_bytes=0
    )
    stores = [Store(c) for c in camera_settings(settings)]
    event = {"id": 1, "segment_id": "source.wav", "start": 1010, "end": 1014}
    for store in stores:
        buffer = video_dir(store.settings) / "buffer"
        buffer.mkdir()
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=green:s=160x120:r=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=16000",
                "-t",
                "10",
                "-c:v",
                "libx264",
                str(buffer / "1000.mkv"),
            ],
            check=True,
        )
        shutil.copy(buffer / "1000.mkv", buffer / "1010.mkv")
        shutil.copy(buffer / "1000.mkv", buffer / "1020.mkv")
        target = video_dir(settings) / clip_name(event, store.settings.camera_id)
        assert make_clip(store.settings, event, target)
        assert 7.5 < duration(target) < 8.5
    stores[0].add_segment("source.wav", 1000, 60, 1)
    stores[0].complete("source.wav", [Event(1010, 1014, 0.8)])
    with TestClient(create_app(settings)) as client:
        result = client.get("/api/events/1/videos?segment=source.wav").json()
        assert len(result["videos"]) == 2
        assert all(v["available"] for v in result["videos"])
        for video in result["videos"]:
            response = client.get(video["url"], headers={"Range": "bytes=0-31"})
            assert response.status_code == 206
            assert len(response.content) == 32
            assert response.headers["content-type"] == "video/mp4"
        assert (
            client.get("/api/video/1?segment=wrong&source=camera1").status_code == 404
        )
        assert (
            client.get(
                "/api/video/1?segment=source.wav&source=../../etc/passwd"
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/video/1?segment=source.wav&source=camera1&camera=unknown"
            ).status_code
            == 404
        )
    assert not make_clip(
        settings, dict(event, start=1050, end=1060), tmp_path / "missing.mp4"
    )
    assert not make_clip(
        settings, dict(event, start=1021, end=1023), tmp_path / "open.mp4"
    )


def test_audio_cleanup_respects_dashboard_policy_for_both_cameras(tmp_path):
    from barkmon.worker import cleanup

    settings = Settings(
        data_dir=tmp_path, camera2_host="192.168.1.206", min_free_bytes=0
    )
    set_retention(settings, 30)
    now = 40 * 86400
    for camera in camera_settings(settings):
        store = Store(camera)
        for name, start in [("expired.wav", 86400), ("keep.wav", 20 * 86400)]:
            (camera.recordings / name).write_bytes(b"audio")
            store.add_segment(name, start, 60, 5)
        cleanup(camera, store, now)
        assert not (camera.recordings / "expired.wav").exists()
        assert (camera.recordings / "keep.wav").exists()
        set_retention(settings, 7)
        cleanup(camera, store, now)
        assert not (camera.recordings / "keep.wav").exists()
        set_retention(settings, 30)


@pytest.mark.parametrize("video_is_newer", [True, False])
def test_storage_cap_evicts_oldest_media_across_formats(tmp_path, video_is_newer):
    from dataclasses import replace
    from barkmon.worker import cleanup

    settings = replace(
        Settings(data_dir=tmp_path), max_storage_bytes=120, min_free_bytes=0
    )
    store = Store(settings)
    audio = settings.recordings / "segment.wav"
    audio.write_bytes(b"a" * 100)
    audio_start = 1000 if video_is_newer else 2000
    store.add_segment(audio.name, audio_start, 60, 100)
    store.complete(audio.name, [Event(audio_start, audio_start + 1, 0.8)])
    video = video_dir(settings) / "clip.mp4"
    video.write_bytes(b"v" * 50)
    timestamp = 2000 if video_is_newer else 1000
    os.utime(video, (timestamp, timestamp))
    cleanup(settings, store, now=2100)
    assert video.exists() is video_is_newer
    assert audio.exists() is not video_is_newer
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == (
            0 if video_is_newer else 1
        )

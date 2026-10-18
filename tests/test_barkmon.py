import os
import time
import wave
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from fastapi.testclient import TestClient

from barkmon.api import create_app
from barkmon.config import Settings
from barkmon.detection import Event, merge_hits
from barkmon.reporting import period, summary, union_seconds
from barkmon.store import Store
from barkmon.worker import cleanup, recover, save_audio


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, min_free_bytes=0)
    store = Store(settings)
    return settings, store


def test_recording_recovery_and_transactional_detection(setup):
    settings, store = setup
    save_audio(settings, store, bytes(32000), 1000)
    recording = next(settings.recordings.glob("*.wav"))
    with wave.open(str(recording)) as stream:
        assert stream.getframerate() == 16000
        assert stream.getnframes() == 16000
    with store.connect() as db:
        db.execute("DELETE FROM segments")
    recover(settings, store)
    events = [Event(1000.1, 1000.5, 0.8)]
    store.complete(recording.name, events)
    store.complete(recording.name, events)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert db.execute("SELECT state FROM segments").fetchone()[0] == "done"


def test_positive_hops_merge_without_overlap_or_counting_quiet_gaps():
    events = merge_hits([(0, 0.5), (0.48, 0.8), (1.44, 0.7)], 1000, 1.6)
    assert len(events) == 2
    assert events[0].start == 1000
    assert events[0].end == pytest.approx(1000.96)
    assert events[0].score == 0.8
    assert events[1].end == 1001.6
    assert union_seconds([(0, 2), (1, 3), (6, 7)]) == 4


def test_coverage_excludes_gaps_failed_analysis_and_rejected_events(setup):
    _, store = setup
    store.add_segment("one.wav", 1000, 60, 100)
    store.add_segment("two.wav", 1120, 60, 100)
    store.complete("one.wav", [Event(1010, 1012, 0.9), Event(1020, 1023, 0.8)])
    with store.connect() as db:
        db.execute("UPDATE events SET review='rejected' WHERE start=1020")
    stats = summary(store, 1000, 1180, 1180)
    assert stats["coverage_percent"] == 66.7
    assert stats["analysis_percent"] == 33.3
    assert stats["gap_seconds"] == 60
    assert stats["barking_seconds"] == 2
    assert stats["episodes"] == 1


def test_episodes_merge_across_segment_boundary(setup):
    _, store = setup
    store.add_segment("one.wav", 1000, 60, 100)
    store.add_segment("two.wav", 1060, 60, 100)
    store.complete("one.wav", [Event(1059.5, 1060, 0.9)])
    store.complete("two.wav", [Event(1060, 1060.48, 0.8)])
    stats = summary(store, 1000, 1120, 1120)
    assert stats["episodes"] == 1
    assert stats["longest_episode_seconds"] == 0.98


def test_dst_report_window_uses_real_elapsed_time(setup):
    settings, _ = setup
    start, end = period(settings, "2026-10-24", "night")
    assert end - start == 10 * 3600  # Clock goes back during this night.
    start, end = period(settings, "2026-03-28", "night")
    assert end - start == 8 * 3600


def test_retention_deletes_audio_and_events_together(setup):
    settings, store = setup
    save_audio(settings, store, bytes(32000), 1000)
    recording = next(settings.recordings.glob("*.wav"))
    store.complete(recording.name, [Event(1000, 1000.48, 0.8)])
    cleanup(settings, store, now=1000 + 8 * 86400)
    assert not recording.exists()
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM segments").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_storage_budget_removes_oldest_first(setup):
    settings, store = setup
    from dataclasses import replace

    settings = replace(settings, max_storage_bytes=33000)
    save_audio(settings, store, bytes(32000), 1000)
    save_audio(settings, store, bytes(32000), 1001)
    cleanup(settings, store, now=1003)
    with store.connect() as db:
        rows = db.execute("SELECT start FROM segments").fetchall()
    assert [r["start"] for r in rows] == [1001]


def test_api_review_playback_reports_and_validation(setup):
    settings, store = setup
    start = datetime(2026, 10, 18, 1, tzinfo=ZoneInfo("Europe/London")).timestamp()
    save_audio(settings, store, bytes(32000), start)
    filename = next(settings.recordings.glob("*.wav")).name
    store.complete(filename, [Event(start, start + 0.48, 0.8)])
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/api/status").json()["recording"] is False
        events = client.get("/api/events?date=2026-10-17").json()
        event_id = events["events"][0]["id"]
        assert events["total"] == 1
        assert client.get(f"/api/audio/{event_id}").content[:4] == b"RIFF"
        assert client.get("/api/audio/99999").status_code == 404
        assert (
            client.patch(
                f"/api/events/{event_id}",
                json={"review": "rejected"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.patch(
                f"/api/events/{event_id}", json={"review": "rejected"}
            ).status_code
            == 200
        )
        assert client.get("/api/summary?date=2026-10-17").json()["episodes"] == 0
        assert (
            client.patch(
                f"/api/events/{event_id}", json={"review": "anything"}
            ).status_code
            == 422
        )
        assert client.get("/api/summary?date=bad").status_code == 422
        assert client.get("/api/events?limit=-1").status_code == 422
        assert "rejected" in client.get("/api/report.csv?date=2026-10-17").text
        assert client.get("/api/report.pdf?date=2026-10-17").content[:5] == b"%PDF-"
        assert "barkmon_worker_up 0" in client.get("/metrics").text
        store.status("worker", "running")
        store.status("recorder", "recording")
        assert client.get("/api/status").json()["recording"] is True
        with store.connect() as db:
            db.execute("UPDATE status SET updated=?", (time.time() - 200,))
        assert client.get("/api/status").json()["recording"] is False
        assert client.get("/api/status").json()["worker_healthy"] is False


def test_actual_yamnet_model_inference():
    path = os.getenv("YAMNET_SMOKE_MODEL")
    if not path:
        pytest.skip("Set YAMNET_SMOKE_MODEL on the Linux worker runtime")
    from barkmon.detection import Yamnet

    model = Yamnet(path, 0.35)
    assert model.samples == 15600
    assert model.classes == [70, 71, 72, 73]
    assert model.detect(np.zeros(16000, dtype=np.float32), 0) == []


def test_recorder_saves_partial_audio_on_disconnect_without_logging_credentials(
    setup, monkeypatch, caplog
):
    import subprocess
    import sys
    import threading
    from barkmon import worker

    settings, store = setup
    stop = threading.Event()
    real_popen = subprocess.Popen
    launched = []

    def fake_camera(argv, **kwargs):
        launched.append(argv)
        # Exercise pipe capture and EOF handling with a real child process.
        return real_popen(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes(32000))"],
            **kwargs,
        )

    real_save = worker.save_audio

    def save_and_stop(*args):
        real_save(*args)
        stop.set()

    monkeypatch.setenv("RTSP_USERNAME", "test@camera")
    monkeypatch.setenv("RTSP_PASSWORD", "secret:password")
    monkeypatch.setattr(worker.subprocess, "Popen", fake_camera)
    monkeypatch.setattr(worker, "save_audio", save_and_stop)
    worker.record(settings, store, stop)
    with store.connect() as db:
        row = db.execute("SELECT * FROM segments").fetchone()
        assert row["duration"] == 1
        assert row["state"] == "pending"
    assert "secret%3Apassword" in launched[0][launched[0].index("-i") + 1]
    assert "secret" not in caplog.text


def test_pipeline_stats_report_retained_audio_processing_states_and_reviews(setup):
    settings, store = setup
    with TestClient(create_app(settings)) as client:
        empty = client.get("/api/pipeline").json()
        assert empty["files"] == 0
        assert empty["processed_percent"] == 0
        assert empty["latest_recording"] is None
        assert empty["oldest_pending_age_seconds"] is None
        store.add_segment("done.wav", 1000, 20, 640044)
        store.add_segment("pending.wav", 1020, 60, 1920044)
        store.add_segment("failed.wav", 1080, 10, 320044)
        store.complete("done.wav", [Event(1001, 1002, 0.8), Event(1005, 1006, 0.7)])
        with store.connect() as db:
            db.execute("UPDATE segments SET state='failed' WHERE id='failed.wav'")
            db.execute("UPDATE events SET review='confirmed' WHERE start=1001")
            db.execute("UPDATE events SET review='rejected' WHERE start=1005")
        store.status("last_processing_seconds", "0.123")
        stats = client.get("/api/pipeline").json()
        assert stats["files"] == 3
        assert stats["audio_bytes"] == 2880132
        assert stats["audio_seconds"] == 90
        assert stats["processed_files"] == 1
        assert stats["pending_files"] == 1
        assert stats["failed_files"] == 1
        assert stats["pending_seconds"] == 60
        assert stats["processed_percent"] == 22.2  # duration-based, not file count
        assert stats["oldest_recording"] == 1000
        assert stats["latest_recording"] == 1090
        assert stats["oldest_pending_age_seconds"] > 0
        assert stats["confirmed"] == 1 and stats["rejected"] == 1
        assert stats["detections"] == 2 and stats["unreviewed"] == 0
        assert stats["database_bytes"] > 0
        assert stats["volume_free_bytes"] > 0
        assert stats["services"]["last_processing_seconds"]["value"] == "0.123"
        assert stats["audio_budget_bytes"] == settings.max_storage_bytes
        assert stats["retention_days"] == settings.retention_days

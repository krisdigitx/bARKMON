import json
import sqlite3
import wave
from dataclasses import replace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from barkmon.acoustics import measurements
from barkmon.api import create_app
from barkmon.config import Settings, camera_settings
from barkmon.detection import Event
from barkmon.store import Store
from barkmon.timeline import episodes, combined_summary, similarity
from barkmon.worker import backfill_one, save_audio


def stores_for(tmp_path):
    config = Settings(data_dir=tmp_path, camera2_host="192.168.1.200", min_free_bytes=0)
    return config, [Store(c) for c in camera_settings(config)]


def add(
    store,
    name,
    start,
    score,
    level,
    count,
    fingerprint,
    review="unreviewed",
    duration=2,
):
    store.add_segment(name, start, 60, 1920044)
    event = Event(
        start,
        start + duration,
        score,
        level,
        level + 3 if level is not None else None,
        count,
        json.dumps(fingerprint) if fingerprint is not None else None,
    )
    store.complete(name, [event])
    with store.connect() as db:
        db.execute("UPDATE events SET review=? WHERE segment_id=?", (review, name))


def test_dedup_selects_audio_strength_not_classifier_confidence(tmp_path):
    settings, stores = stores_for(tmp_path)
    pattern = [0, 0.1, 0.7, 1, 0.6, 0.05, 0, 0.3, 1, 0.8, 0.1, 0] * 4
    add(stores[0], "a.wav", 1000, 0.99, -30, 3, pattern)
    add(stores[1], "b.wav", 1000.2, 0.5, -12, 3, pattern)
    values, _ = episodes(stores, settings, 990, 1100)
    assert len(values) == 1
    event = values[0]
    assert event["bark_count"] == 3  # not 6
    assert event["strongest_camera"] == "camera2"
    assert event["rms_dbfs"] == -12
    assert event["duplicate_detections"] == 1
    assert event["dedup_method"] == "time + audio envelope"
    assert len(event["clips"]) == 2
    assert event["duration"] == 2.2
    summary = combined_summary(stores, settings, 990, 1100, 1100)
    assert summary["recorded_seconds"] < 61  # overlapping coverage is not doubled
    assert summary["estimated_barks"] == 3


def test_separated_barks_remain_separate_episodes_and_rejections_are_excluded(tmp_path):
    settings, stores = stores_for(tmp_path)
    pattern = [0, 1, 0.5, 0] * 10
    add(stores[0], "a.wav", 1000, 0.9, -20, 1, pattern)
    add(stores[1], "b.wav", 1010, 0.9, -10, 1, pattern)
    add(stores[1], "rejected.wav", 1020, 0.9, -2, 5, pattern, "rejected")
    result, _ = episodes(stores, settings, 990, 1100)
    assert len(result) == 2
    assert sum(e["bark_count"] for e in result) == 2
    assert all(e["duplicate_detections"] == 0 for e in result)


def test_dissimilar_overlapping_audio_is_not_deduplicated(tmp_path):
    settings, stores = stores_for(tmp_path)
    add(stores[0], "a.wav", 1000, 0.8, -30, 2, [0] * 20 + [1] * 20)
    add(stores[1], "b.wav", 1000, 0.8, -15, 3, [1] * 20 + [0] * 20)
    assert (
        similarity(
            {
                "start": 1000,
                "end": 1002,
                "fingerprint": json.dumps([0] * 20 + [1] * 20),
            },
            {
                "start": 1000,
                "end": 1002,
                "fingerprint": json.dumps([1] * 20 + [0] * 20),
            },
            0.1,
        )
        < 0.85
    )
    result, _ = episodes(stores, replace(settings, dedup_seconds=0.1), 990, 1100)
    assert result[0]["bark_count"] == 5
    assert result[0]["duplicate_detections"] == 0


def test_legacy_measurements_are_unknown_not_fabricated_and_backfill_preserves_review(
    tmp_path,
):
    settings, stores = stores_for(tmp_path)
    store = stores[0]
    save_audio(store.settings, store, bytes(32000), 1000)
    path = next(store.settings.recordings.glob("*.wav"))
    store.complete(path.name, [Event(1000, 1000.48, 0.9)])
    with store.connect() as db:
        db.execute("UPDATE events SET review='confirmed'")
    before, _ = episodes(stores, settings, 990, 1100)
    assert before[0]["rms_dbfs"] is None and before[0]["strongest_camera"] is None
    assert backfill_one(store)
    with store.connect() as db:
        row = db.execute("SELECT * FROM events").fetchone()
        assert row["review"] == "confirmed"
        assert row["rms_dbfs"] == -120
        assert row["fingerprint"] is not None


def test_relative_levels_and_pulse_estimates():
    rate = 16000
    audio = np.zeros(rate * 2, dtype=np.float32)
    audio[1600:3200] = 0.5
    audio[9600:11200] = 0.5
    result = measurements(audio, 0, 2)
    assert result["peak_dbfs"] == pytest.approx(-6.02, abs=0.01)
    assert result["rms_dbfs"] < result["peak_dbfs"]
    assert result["bark_count"] == 2


def test_schema_migrates_existing_database_without_losing_events(tmp_path):
    path = tmp_path / "barkmon.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE events(id INTEGER PRIMARY KEY,segment_id TEXT,start REAL,end REAL,score REAL,review TEXT); INSERT INTO events VALUES(7,'a',1,2,.8,'confirmed');"
        )
    store = Store(Settings(data_dir=tmp_path))
    with store.connect() as db:
        row = db.execute("SELECT * FROM events").fetchone()
        assert (
            row["id"] == 7 and row["review"] == "confirmed" and row["rms_dbfs"] is None
        )


def test_combined_api_clips_reports_and_overview(tmp_path):
    settings, stores = stores_for(tmp_path)
    for index, store in enumerate(stores):
        save_audio(store.settings, store, b"\x00\x10" * 16000 * 3, 1000)
        name = next(store.settings.recordings.glob("*.wav")).name
        store.complete(
            name,
            [
                Event(
                    1000,
                    1001,
                    0.8,
                    -20 + index * 10,
                    -10,
                    1,
                    json.dumps([0, 1, 0.5, 0] * 5),
                )
            ],
        )
    with TestClient(create_app(settings)) as client:
        result = client.get("/api/timeline?date=1970-01-01&mode=day").json()
        assert (
            result["total"] == 1
            and result["events"][0]["strongest_camera"] == "camera2"
        )
        stats = client.get("/api/timeline/summary?date=1970-01-01&mode=day").json()
        assert stats["estimated_barks"] == 1
        assert client.get("/api/overview").json()["pipeline"]["files"] == 2
        response = client.get("/api/clip/1?camera=camera2")
        assert response.content[:4] == b"RIFF"
        import io

        with wave.open(io.BytesIO(response.content)) as wav:
            assert wav.getnframes() == 32000  # episode plus available 1s context
        assert client.get("/api/clip/999?camera=camera2").status_code == 404
        assert client.get("/api/timeline?camera=unknown").status_code == 404
        assert client.get(
            "/api/report.pdf?camera=all&date=1970-01-01&mode=week"
        ).content.startswith(b"%PDF")
        assert (
            "Camera 2" in client.get("/api/timeline.csv?date=1970-01-01&mode=day").text
        )


def test_scheduler_uses_completed_nights_weeks_and_is_idempotent(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from barkmon.report_archive import due_periods, generate_due_reports

    settings, stores = stores_for(tmp_path)
    now = datetime(2026, 10, 19, 7, 15, tzinfo=ZoneInfo("Europe/London")).timestamp()
    due = due_periods(settings, now)
    assert due[0][1] == "2026-10-18"
    assert due[1][1] == "2026-10-12"
    generated = generate_due_reports(settings, stores, now)
    assert len(generated) == 2
    assert generate_due_reports(settings, stores, now) == []
    for path in (settings.data_dir / "reports").glob("*.pdf"):
        assert path.read_bytes().startswith(b"%PDF")
    with TestClient(create_app(settings)) as client:
        reports = client.get("/api/reports").json()["reports"]
        assert len(reports) == 2
        assert client.get(reports[0]["url"]).content.startswith(b"%PDF")
        assert client.get("/api/reports/not-a-report.pdf").status_code == 404


def test_night_scheduler_waits_for_cutoff_and_archive_waits_for_analysis(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from barkmon.report_archive import due_periods, generate_due_reports

    settings, stores = stores_for(tmp_path)
    before = datetime(2026, 10, 19, 7, 5, tzinfo=ZoneInfo("Europe/London")).timestamp()
    assert due_periods(settings, before)[0][1] == "2026-10-17"
    stores[0].add_segment("pending.wav", before - 60, 60, 1920044)
    assert generate_due_reports(settings, stores, before) == []


def test_no_double_count_across_segment_boundary_and_clipped_counts_unknown(tmp_path):
    settings, stores = stores_for(tmp_path)
    pattern = [0, 1, 0.3, 0] * 10
    add(stores[0], "a.wav", 1000, 0.8, -20, 1, pattern, duration=1)
    add(stores[0], "b.wav", 1001, 0.8, -20, 1, pattern, duration=1)
    add(stores[1], "c.wav", 1000, 0.8, -10, 2, pattern * 2, duration=2)
    result, _ = episodes(stores, settings, 999, 1003)
    assert len(result) == 1
    # Each camera estimates two pulses for the same incident, never four.
    assert result[0]["bark_count"] == 2
    clipped, _ = episodes(stores, settings, 1000.5, 1001.5)
    assert clipped[0]["bark_count"] is None


def test_saved_clip_links_never_resolve_to_a_reused_event_id(tmp_path):
    settings, stores = stores_for(tmp_path)
    store = stores[0]
    save_audio(store.settings, store, bytes(32000), 1000)
    first = next(store.settings.recordings.glob("*.wav")).name
    store.complete(first, [Event(1000, 1000.48, 0.8, -20, -10, 1, "[0,1,0]")])
    with TestClient(create_app(settings)) as client:
        old_url = client.get("/api/timeline?date=1970-01-01&mode=day").json()["events"][
            0
        ]["clips"][0]["url"]
        assert "segment=" in old_url
        assert client.get(old_url).status_code == 200
        store.add_segment("replacement.wav", 2000, 60, 1920044)
        with store.connect() as db:
            db.execute("UPDATE events SET segment_id='replacement.wav' WHERE id=1")
        assert client.get(old_url).status_code == 404

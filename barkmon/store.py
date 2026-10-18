import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
 id TEXT PRIMARY KEY, start REAL NOT NULL, duration REAL NOT NULL,
 bytes INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
 start REAL NOT NULL, end REAL NOT NULL, score REAL NOT NULL,
 review TEXT NOT NULL DEFAULT 'unreviewed'
);
CREATE INDEX IF NOT EXISTS events_start ON events(start);
CREATE INDEX IF NOT EXISTS segments_start ON segments(start);
CREATE TABLE IF NOT EXISTS status (
 name TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL
);
"""


class Store:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data_dir / "barkmon.sqlite3"
        settings.recordings.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def status(self, name, value):
        with self.connect() as db:
            db.execute(
                "INSERT INTO status VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (name, value, time.time()),
            )

    def statuses(self):
        with self.connect() as db:
            return {r["name"]: dict(r) for r in db.execute("SELECT * FROM status")}

    def add_segment(self, name, start, duration, size):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO segments(id,start,duration,bytes) VALUES(?,?,?,?)",
                (name, start, duration, size),
            )

    def complete(self, segment_id, events):
        # One transaction prevents partial detections or duplicate events on retry.
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE segment_id=?", (segment_id,))
            db.executemany(
                "INSERT INTO events(segment_id,start,end,score) VALUES(?,?,?,?)",
                [(segment_id, e.start, e.end, e.score) for e in events],
            )
            db.execute("UPDATE segments SET state='done' WHERE id=?", (segment_id,))

    def inventory(self):
        """Retained/indexed recordings only; excludes the in-flight audio buffer."""
        with self.connect() as db:
            row = db.execute("""
                SELECT count(*) AS files, coalesce(sum(bytes),0) AS audio_bytes,
                    coalesce(sum(duration),0) AS audio_seconds,
                    coalesce(sum(state='done'),0) AS processed_files,
                    coalesce(sum(state='pending'),0) AS pending_files,
                    coalesce(sum(state='failed'),0) AS failed_files,
                    coalesce(sum(CASE WHEN state='done' THEN duration ELSE 0 END),0) AS processed_seconds,
                    coalesce(sum(CASE WHEN state='pending' THEN duration ELSE 0 END),0) AS pending_seconds,
                    min(start) AS oldest_recording,
                    max(start+duration) AS latest_recording,
                    min(CASE WHEN state='pending' THEN start END) AS oldest_pending
                FROM segments
            """).fetchone()
            reviews = dict(
                db.execute(
                    "SELECT review,count(*) FROM events GROUP BY review"
                ).fetchall()
            )
        return {
            **dict(row),
            "detections": sum(reviews.values()),
            "confirmed": reviews.get("confirmed", 0),
            "rejected": reviews.get("rejected", 0),
            "unreviewed": reviews.get("unreviewed", 0),
        }

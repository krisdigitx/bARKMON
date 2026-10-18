import csv
import io
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from barkmon.config import Settings, camera_settings
from barkmon.reporting import period, summary
from barkmon.store import Store

STATIC = Path(__file__).parent / "static"


def create_app(settings=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.stores = {
            camera.camera_id: Store(camera) for camera in camera_settings(settings)
        }
        yield

    app = FastAPI(title="Barkmon", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def selected_store(camera: str = "camera1"):
        store = app.state.stores.get(camera)
        if store is None:
            raise HTTPException(404, "Camera not configured")
        return store

    def check_origin(request):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-origin requests are not allowed")

    @app.get("/api/cameras")
    def cameras():
        result = []
        now = time.time()
        for camera_id, store in app.state.stores.items():
            state = store.statuses()
            recorder = state.get("recorder", {})
            result.append(
                {
                    "id": camera_id,
                    "name": store.settings.camera_name,
                    "host": store.settings.camera_host,
                    "recording": recorder.get("value") == "recording"
                    and now - recorder.get("updated", 0) < 30,
                    "detail": state.get("connection_detail", {}).get(
                        "value", "Waiting for worker"
                    ),
                }
            )
        return {"cameras": result}

    @app.post("/api/camera/check", status_code=202)
    def reconnect(request: Request, store: Store = Depends(selected_store)):
        check_origin(request)
        store.status("reconnect_request", "requested")
        return {"status": "requested", "camera": store.settings.camera_id}

    def bounds(day, mode):
        try:
            return period(settings, day, mode)
        except ValueError:
            raise HTTPException(422, "Invalid date; use YYYY-MM-DD") from None

    @app.get("/")
    def dashboard():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    def health(store: Store = Depends(selected_store)):
        with store.connect() as db:
            db.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/api/status")
    def status(store: Store = Depends(selected_store)):
        settings = store.settings
        space = shutil.disk_usage(settings.data_dir)
        states = store.statuses()
        worker = states.get("worker", {})
        recorder = states.get("recorder", {})
        return {
            "camera_id": settings.camera_id,
            "camera_host": settings.camera_host,
            "services": states,
            "worker_healthy": worker.get("value") == "running"
            and time.time() - worker.get("updated", 0) < 90,
            "recording": recorder.get("value") == "recording"
            and time.time() - recorder.get("updated", 0) < 30,
            "free_bytes": space.free,
            "total_bytes": space.total,
            "threshold": settings.threshold,
            "timezone": settings.timezone,
            "retention_days": settings.retention_days,
            "night_start": settings.night_start,
            "night_end": settings.night_end,
            "now": time.time(),
        }

    @app.get("/api/pipeline")
    def pipeline(store: Store = Depends(selected_store)):
        settings = store.settings
        stats = store.inventory()
        space = shutil.disk_usage(settings.data_dir)
        now = time.time()
        database_bytes = 0
        for path in settings.data_dir.glob("barkmon.sqlite3*"):
            try:
                database_bytes += path.stat().st_size
            except FileNotFoundError:
                # SQLite may checkpoint/remove WAL files between listing and stat.
                continue
        stats.update(
            {
                "audio_budget_bytes": settings.max_storage_bytes,
                "audio_budget_percent": round(
                    100 * stats["audio_bytes"] / settings.max_storage_bytes, 1
                ),
                "volume_used_bytes": space.used,
                "volume_free_bytes": space.free,
                "volume_total_bytes": space.total,
                "database_bytes": database_bytes,
                "processed_percent": round(
                    100 * stats["processed_seconds"] / stats["audio_seconds"], 1
                )
                if stats["audio_seconds"]
                else 0,
                "oldest_pending_age_seconds": max(0, now - stats["oldest_pending"])
                if stats["oldest_pending"] is not None
                else None,
                "latest_recording_age_seconds": max(0, now - stats["latest_recording"])
                if stats["latest_recording"] is not None
                else None,
                "retention_days": settings.retention_days,
                "segment_seconds": settings.segment_seconds,
                "format": "16 kHz · mono · 16-bit PCM WAV",
                "services": store.statuses(),
            }
        )
        return stats

    @app.get("/api/summary")
    def get_summary(
        date: str | None = None,
        mode: Literal["night", "day", "week"] = "night",
        store: Store = Depends(selected_store),
    ):
        return summary(store, *bounds(date, mode), time.time())

    @app.get("/api/events")
    def events(
        date: str | None = None,
        mode: Literal["night", "day", "week"] = "night",
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        store: Store = Depends(selected_store),
    ):
        start, end = bounds(date, mode)
        with store.connect() as db:
            total = db.execute(
                "SELECT count(*) FROM events WHERE start<? AND end>?", (end, start)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT e.*, s.start AS segment_start FROM events e JOIN segments s ON s.id=e.segment_id WHERE e.start<? AND e.end>? ORDER BY e.start DESC LIMIT ? OFFSET ?",
                (end, start, limit, offset),
            ).fetchall()
        return {"total": total, "events": [dict(r) for r in rows]}

    class Review(BaseModel):
        review: Literal["confirmed", "rejected", "unreviewed"]

    @app.patch("/api/events/{event_id}")
    def review(
        event_id: int,
        body: Review,
        request: Request,
        store: Store = Depends(selected_store),
    ):
        # Prevent cross-origin browser writes to the internal dashboard.
        check_origin(request)
        with store.connect() as db:
            cursor = db.execute(
                "UPDATE events SET review=? WHERE id=?", (body.review, event_id)
            )
            if not cursor.rowcount:
                raise HTTPException(404, "Event no longer available")
        return {"review": body.review}

    @app.get("/api/audio/{event_id}")
    def audio(event_id: int, store: Store = Depends(selected_store)):
        with store.connect() as db:
            row = db.execute(
                "SELECT segment_id FROM events WHERE id=?", (event_id,)
            ).fetchone()
        if not row:
            raise HTTPException(404, "Event no longer available")
        path = store.settings.recordings / row["segment_id"]
        if not path.is_file():
            raise HTTPException(404, "Recording has expired")
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/report.csv")
    def report_csv(
        date: str | None = None,
        mode: Literal["night", "day", "week"] = "night",
        store: Store = Depends(selected_store),
    ):
        start, end = bounds(date, mode)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "Camera",
                "Start",
                "End",
                "Barking seconds (estimated)",
                "YAMNet score",
                "Review",
            ]
        )
        with store.connect() as db:
            for row in db.execute(
                "SELECT * FROM events WHERE start<? AND end>? ORDER BY start",
                (end, start),
            ):
                writer.writerow(
                    [
                        store.settings.camera_name,
                        datetime.fromtimestamp(
                            row["start"], ZoneInfo(settings.timezone)
                        ).isoformat(),
                        datetime.fromtimestamp(
                            row["end"], ZoneInfo(settings.timezone)
                        ).isoformat(),
                        round(min(end, row["end"]) - max(start, row["start"]), 2),
                        round(row["score"], 3),
                        row["review"],
                    ]
                )
        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="barkmon-events.csv"'
            },
        )

    @app.get("/api/report.pdf")
    def report_pdf(
        date: str | None = None,
        mode: Literal["night", "day", "week"] = "night",
        store: Store = Depends(selected_store),
    ):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen.canvas import Canvas

        start, end = bounds(date, mode)
        stats = summary(store, start, end, time.time())
        buffer = io.BytesIO()
        canvas = Canvas(buffer, pagesize=A4)
        zone = ZoneInfo(settings.timezone)

        def stamp(timestamp):
            return datetime.fromtimestamp(timestamp, zone).strftime("%d %b %Y %H:%M %Z")

        lines = [
            "Barkmon monitoring report",
            f"{store.settings.camera_name} ({store.settings.camera_host})",
            f"{stamp(start)} to {stamp(end)}",
            f"Generated: {stamp(time.time())}",
            "",
            f"Detected episodes: {stats['episodes']}",
            f"Estimated barking duration: {stats['barking_seconds']} seconds",
            f"Longest episode: {stats['longest_episode_seconds']} seconds",
            f"Recording coverage of elapsed period: {stats['coverage_percent']}%",
            f"Analysed coverage: {stats['analysis_percent']}%",
            f"Unrecorded elapsed time: {stats['gap_seconds']} seconds",
            f"Pending / failed segments: {stats['pending_segments']} / {stats['failed_segments']}",
            "",
            "Automated YAMNet detections are estimates, not individual bark counts.",
            "Rejected detections are excluded from the totals. No calibrated noise level is measured.",
            f"Reports include only retained data (retention: {settings.retention_days} days).",
            "",
            "Detection timeline (local time, estimated seconds, score, review):",
        ]
        with store.connect() as db:
            for row in db.execute(
                "SELECT * FROM events WHERE start<? AND end>? ORDER BY start",
                (end, start),
            ):
                lines.append(
                    f"{stamp(row['start'])}   {min(end, row['end']) - max(start, row['start']):.2f}s   {row['score']:.2f}   {row['review']}"
                )
        y = 800
        for line in lines:
            if y < 45:
                canvas.showPage()
                y = 800
            canvas.setFont("Helvetica", 10)
            canvas.drawString(40, y, line)
            y -= 18
        canvas.save()
        return Response(
            buffer.getvalue(),
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="barkmon-report.pdf"'
            },
        )

    @app.get("/metrics")
    def metrics(store: Store = Depends(selected_store)):
        states = store.statuses()
        worker = states.get("worker", {})
        recorder = states.get("recorder", {})
        with store.connect() as db:
            pending = db.execute(
                "SELECT count(*) FROM segments WHERE state='pending'"
            ).fetchone()[0]
            failed = db.execute(
                "SELECT count(*) FROM segments WHERE state='failed'"
            ).fetchone()[0]
        healthy = (
            worker.get("value") == "running"
            and time.time() - worker.get("updated", 0) < 90
        )
        recording = (
            recorder.get("value") == "recording"
            and time.time() - recorder.get("updated", 0) < 30
        )
        camera_metrics = []
        for camera_id, camera_store in app.state.stores.items():
            state = camera_store.statuses()
            rec = state.get("recorder", {})
            active = (
                rec.get("value") == "recording"
                and time.time() - rec.get("updated", 0) < 30
            )
            camera_metrics.append(
                f'barkmon_camera_recording{{camera="{camera_id}"}} {int(active)}'
            )
            camera_metrics.append(
                f'barkmon_camera_pending_segments{{camera="{camera_id}"}} {camera_store.inventory()["pending_files"]}'
            )
        return Response(
            f"barkmon_worker_up {int(healthy)}\nbarkmon_recording {int(recording)}\nbarkmon_pending_segments {pending}\nbarkmon_failed_segments {failed}\nbarkmon_storage_free_bytes {shutil.disk_usage(settings.data_dir).free}\n"
            + "\n".join(camera_metrics)
            + "\n",
            media_type="text/plain",
        )

    return app


app = create_app()

import csv
import io
import shutil
import time
import wave
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from barkmon.config import Settings, camera_settings
from barkmon.reporting import period, summary
from barkmon.store import Store
from barkmon.timeline import combined_summary, episodes
from barkmon.reports import render_pdf

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

    @app.middleware("http")
    async def fresh_dashboard(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def selected_store(camera: str = "camera1"):
        store = app.state.stores.get(camera)
        if store is None:
            raise HTTPException(404, "Camera not configured")
        return store

    def check_origin(request):
        origin = request.headers.get("origin")
        allowed = {str(request.base_url).rstrip("/")}
        # The tunnel terminates TLS outside this pod. Trust only the configured
        # public origin, and only when the request is addressed to that host.
        if settings.public_base_url:
            public = urlsplit(settings.public_base_url)
            if request.headers.get("host", "").lower() == public.netloc.lower():
                allowed.add(settings.public_base_url.rstrip("/"))
        if origin and origin.rstrip("/") not in allowed:
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

    class DetectionNote(BaseModel):
        note: str = Field(max_length=4000)

    @app.put("/api/events/{event_id}/note")
    def save_note(
        event_id: int,
        body: DetectionNote,
        request: Request,
        segment: str,
        store: Store = Depends(selected_store),
    ):
        check_origin(request)
        with store.connect() as db:
            cursor = db.execute(
                "UPDATE events SET note=? WHERE id=? AND segment_id=?",
                (body.note, event_id, segment),
            )
            if not cursor.rowcount:
                raise HTTPException(404, "Event no longer available")
        return {"note": body.note}

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

    def timeline_stores(camera):
        if camera == "all":
            return list(app.state.stores.values())
        store = app.state.stores.get(camera)
        if store is None:
            raise HTTPException(404, "Camera not configured")
        return [store]

    @app.get("/api/timeline")
    def timeline(
        date: str | None = None,
        mode: Literal["night", "day", "week"] = "night",
        camera: str = "all",
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        values, _ = episodes(timeline_stores(camera), settings, *bounds(date, mode))
        values.reverse()
        return {"total": len(values), "events": values[offset : offset + limit]}

    @app.get("/api/timeline/summary")
    def timeline_summary(
        date: str | None = None,
        mode: Literal["night", "day", "week"] = "night",
        camera: str = "all",
    ):
        return combined_summary(
            timeline_stores(camera), settings, *bounds(date, mode), time.time()
        )

    @app.get("/api/overview")
    def overview():
        stores = list(app.state.stores.values())
        states = [status(store) for store in stores]
        pipelines = [pipeline(store) for store in stores]
        combined = dict(pipelines[0])
        for key in [
            "files",
            "audio_bytes",
            "audio_seconds",
            "processed_files",
            "pending_files",
            "failed_files",
            "processed_seconds",
            "pending_seconds",
            "database_bytes",
            "audio_budget_bytes",
            "detections",
            "confirmed",
            "rejected",
            "unreviewed",
        ]:
            combined[key] = sum(p[key] for p in pipelines)
        for key in ["oldest_recording", "oldest_pending"]:
            combined[key] = min(
                (p[key] for p in pipelines if p[key] is not None), default=None
            )
        combined["latest_recording"] = max(
            (
                p["latest_recording"]
                for p in pipelines
                if p["latest_recording"] is not None
            ),
            default=None,
        )
        combined["audio_budget_percent"] = round(
            100 * combined["audio_bytes"] / combined["audio_budget_bytes"], 1
        )
        combined["processed_percent"] = (
            round(100 * combined["processed_seconds"] / combined["audio_seconds"], 1)
            if combined["audio_seconds"]
            else 0
        )
        for key, source in [
            ("latest_recording_age_seconds", "latest_recording"),
            ("oldest_pending_age_seconds", "oldest_pending"),
        ]:
            combined[key] = (
                max(0, time.time() - combined[source])
                if combined[source] is not None
                else None
            )
        count = sum(s["recording"] for s in states)
        services = {
            "recorder": {"value": f"{count}/{len(states)} cameras recording"},
            "detector": {"value": "Shared detector"},
        }
        timing = max(
            (
                p["services"]["last_processing_seconds"]
                for p in pipelines
                if "last_processing_seconds" in p["services"]
            ),
            key=lambda v: v["updated"],
            default=None,
        )
        if timing:
            services["last_processing_seconds"] = timing
        combined["services"] = services
        state = dict(
            states[0],
            camera_id="all",
            camera_host="All cameras",
            recording=count > 0,
            worker_healthy=all(s["worker_healthy"] for s in states),
            services=services,
            recording_cameras=count,
            total_cameras=len(states),
        )
        return {"status": state, "pipeline": combined}

    @app.get("/api/clip/{event_id}")
    def clip(
        event_id: int,
        segment: str | None = None,
        store: Store = Depends(selected_store),
    ):
        with store.connect() as db:
            row = db.execute(
                "SELECT e.*,s.start AS segment_start FROM events e JOIN segments s ON s.id=e.segment_id WHERE e.id=?",
                (event_id,),
            ).fetchone()
        if not row or (segment is not None and row["segment_id"] != segment):
            raise HTTPException(404, "Source detection no longer retained")
        try:
            with wave.open(
                str(store.settings.recordings / row["segment_id"])
            ) as source:
                rate = source.getframerate()
                begin = max(0, int((row["start"] - row["segment_start"] - 1) * rate))
                finish = min(
                    source.getnframes(),
                    int((row["end"] - row["segment_start"] + 1) * rate),
                )
                source.setpos(begin)
                pcm = source.readframes(max(0, finish - begin))
                output = io.BytesIO()
                with wave.open(output, "wb") as target:
                    target.setparams(source.getparams())
                    target.writeframes(pcm)
        except (OSError, EOFError, wave.Error):
            raise HTTPException(404, "Source audio expired or unavailable") from None
        return Response(
            output.getvalue(),
            media_type="audio/wav",
            headers={
                "Content-Disposition": f'inline; filename="{store.settings.camera_id}-event-{event_id}.wav"'
            },
        )

    @app.get("/api/report.pdf")
    def report_pdf(
        request: Request,
        date: str | None = None,
        mode: Literal["night", "day", "week"] = "night",
        camera: str = "all",
    ):
        start, end = bounds(date, mode)
        data = render_pdf(
            timeline_stores(camera),
            settings,
            start,
            end,
            time.time(),
            mode,
            settings.report_base_url or str(request.base_url),
        )
        return Response(
            data,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="dog-monitor-{mode}-{datetime.fromtimestamp(start, ZoneInfo(settings.timezone)).date()}.pdf"'
            },
        )

    @app.get("/api/timeline.csv")
    def timeline_csv(
        date: str | None = None,
        mode: Literal["night", "day", "week"] = "night",
        camera: str = "all",
    ):
        values, _ = episodes(timeline_stores(camera), settings, *bounds(date, mode))
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "Episode",
                "Start",
                "End",
                "Span seconds",
                "Active seconds",
                "Estimated bark pulses",
                "Pulses/min within episode",
                "Strongest measured camera",
                "RMS dBFS",
                "Peak dBFS",
                "Deduplication",
                "Sources",
            ]
        )
        for event in values:
            writer.writerow(
                [
                    event["id"],
                    datetime.fromtimestamp(
                        event["start"], ZoneInfo(settings.timezone)
                    ).isoformat(),
                    datetime.fromtimestamp(
                        event["end"], ZoneInfo(settings.timezone)
                    ).isoformat(),
                    event["duration"],
                    event["active_seconds"],
                    event["bark_count"],
                    event["barks_per_minute"],
                    event["strongest_camera_name"],
                    event["rms_dbfs"],
                    event["peak_dbfs"],
                    event["dedup_method"],
                    "; ".join(
                        f"{c['camera_id']}:{c['event_id']}" for c in event["clips"]
                    ),
                ]
            )
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="dog-monitor-timeline.csv"'
            },
        )

    @app.get("/api/reports")
    def archived_reports():
        directory = settings.data_dir / "reports"
        files = (
            sorted(
                directory.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if directory.exists()
            else []
        )
        return {
            "retention_days": settings.report_retention_days,
            "reports": [
                {
                    "name": p.name,
                    "generated": p.stat().st_mtime,
                    "bytes": p.stat().st_size,
                    "url": "/api/reports/" + p.name,
                }
                for p in files
            ],
        }

    @app.get("/api/reports/{filename}")
    def archived_report(filename: str):
        if not re.fullmatch(r"(night|week)-[0-9]{4}-[0-9]{2}-[0-9]{2}\.pdf", filename):
            raise HTTPException(404, "Report not found")
        path = settings.data_dir / "reports" / filename
        if not path.is_file():
            raise HTTPException(404, "Report no longer retained")
        return FileResponse(path, media_type="application/pdf", filename=filename)

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

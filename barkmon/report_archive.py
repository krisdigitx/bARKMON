"""Scheduled combined PDF snapshots retained independently of source audio."""

from datetime import datetime, timedelta
import time
from zoneinfo import ZoneInfo

from barkmon.reporting import period
from barkmon.reports import render_pdf


def due_periods(settings, now):
    local = datetime.fromtimestamp(now, ZoneInfo(settings.timezone))
    day = local.date() - timedelta(days=int(settings.night_end <= settings.night_start))
    start, end = period(settings, day.isoformat(), "night")
    if now < end + 600:
        day -= timedelta(days=1)
        start, end = period(settings, day.isoformat(), "night")
    result = [("night", day.isoformat(), start, end)]
    monday = local.date() - timedelta(days=local.weekday())
    week = monday - timedelta(days=7)
    start, end = period(settings, week.isoformat(), "week")
    if now < end + 600:
        week -= timedelta(days=7)
        start, end = period(settings, week.isoformat(), "week")
    result.append(("week", week.isoformat(), start, end))
    return result


def generate_due_reports(settings, stores, now=None):
    now = time.time() if now is None else now
    directory = settings.data_dir / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    # Let current and legacy analysis finish before taking a scheduled snapshot.
    ready = True
    for store in stores:
        with store.connect() as db:
            if (
                db.execute(
                    "SELECT 1 FROM segments WHERE state='pending' LIMIT 1"
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM events WHERE fingerprint IS NULL LIMIT 1"
                ).fetchone()
            ):
                ready = False
    generated = []
    if ready:
        for mode, day, start, end in due_periods(settings, now):
            path = directory / f"{mode}-{day}.pdf"
            if path.exists():
                continue
            content = render_pdf(
                stores, settings, start, end, now, mode, settings.report_base_url
            )
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(path)
            generated.append(path.name)
    paths = sorted(directory.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in paths)
    for path in paths:
        size = path.stat().st_size
        if (
            now - path.stat().st_mtime > settings.report_retention_days * 86400
            or total > settings.report_max_bytes
        ):
            path.unlink()
            total -= size
    return generated


def report_loop(settings, stores, stop):
    while not stop.is_set():
        try:
            generated = generate_due_reports(settings, stores)
            if generated:
                stores[0].status("reports", "Generated " + ", ".join(generated))
        except Exception as error:
            stores[0].status(
                "reports", "Report generation failed: " + type(error).__name__
            )
        stop.wait(60)

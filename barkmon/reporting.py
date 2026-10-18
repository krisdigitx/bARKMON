from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def period(settings, day, mode):
    zone = ZoneInfo(settings.timezone)
    now = datetime.now(zone)
    if day is None and mode == "week":
        day = now.date() - timedelta(days=6)
    elif day is None:
        day = (
            (now.date() - timedelta(days=1))
            if mode == "night" and now.hour < settings.night_start
            else now.date()
        )
    else:
        day = date.fromisoformat(day)
    if mode == "night":
        first = datetime.combine(day, time(settings.night_start), zone)
        end_day = day + timedelta(
            days=1 if settings.night_end <= settings.night_start else 0
        )
        last = datetime.combine(end_day, time(settings.night_end), zone)
    else:
        first = datetime.combine(day, time(), zone)
        last = datetime.combine(
            day + timedelta(days=7 if mode == "week" else 1), time(), zone
        )
    return first.timestamp(), last.timestamp()


def union_seconds(intervals):
    total = 0.0
    end = float("-inf")
    for start, stop in sorted(intervals):
        total += max(0, stop - max(start, end))
        end = max(end, stop)
    return total


def summary(store, start, end, now):
    elapsed_end = min(end, now)
    with store.connect() as db:
        segments = db.execute(
            "SELECT * FROM segments WHERE start < ? AND start+duration > ? ORDER BY start",
            (end, start),
        ).fetchall()
        events = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM events WHERE start < ? AND end > ? ORDER BY start",
                (end, start),
            )
        ]
    accepted = [e for e in events if e["review"] != "rejected"]
    covered = union_seconds(
        [
            (max(start, r["start"]), min(elapsed_end, r["start"] + r["duration"]))
            for r in segments
            if r["start"] < elapsed_end
        ]
    )
    analysed = union_seconds(
        [
            (max(start, r["start"]), min(elapsed_end, r["start"] + r["duration"]))
            for r in segments
            if r["state"] == "done" and r["start"] < elapsed_end
        ]
    )
    seconds = union_seconds(
        [(max(start, e["start"]), min(end, e["end"])) for e in accepted]
    )
    # Merge adjacent episodes across recording boundaries.
    episodes = []
    for event in accepted:
        a, b = max(start, event["start"]), min(end, event["end"])
        if episodes and a <= episodes[-1][1] + 0.001:
            episodes[-1][1] = max(episodes[-1][1], b)
        else:
            episodes.append([a, b])
    elapsed = max(0, elapsed_end - start)
    hourly = []
    cursor = start
    while cursor < end:
        stop = min(end, cursor + 3600)
        hourly.append(
            {
                "start": cursor,
                "seconds": union_seconds(
                    [
                        (max(cursor, e["start"]), min(stop, e["end"]))
                        for e in accepted
                        if e["start"] < stop and e["end"] > cursor
                    ]
                ),
            }
        )
        cursor = stop
    return {
        "start": start,
        "end": end,
        "elapsed_seconds": elapsed,
        "episodes": len(episodes),
        "barking_seconds": round(seconds, 2),
        "longest_episode_seconds": round(
            max((b - a for a, b in episodes), default=0), 2
        ),
        "recorded_seconds": round(covered, 2),
        "analysed_seconds": round(analysed, 2),
        "coverage_percent": round(100 * covered / elapsed, 1) if elapsed else 0,
        "analysis_percent": round(100 * analysed / elapsed, 1) if elapsed else 0,
        "gap_seconds": round(max(0, elapsed - covered), 2),
        "pending_segments": sum(r["state"] == "pending" for r in segments),
        "failed_segments": sum(r["state"] == "failed" for r in segments),
        "reviewed": sum(e["review"] != "unreviewed" for e in events),
        "hourly": hourly,
    }

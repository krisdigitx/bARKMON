"""Combined activity episodes with retained source evidence and heuristic deduplication."""

import hashlib
import json

import numpy as np

from barkmon.reporting import union_seconds


def source_events(stores, start, end, padding=0):
    result = []
    for store in stores:
        with store.connect() as db:
            for row in db.execute(
                "SELECT e.*,s.start AS segment_start FROM events e JOIN segments s ON s.id=e.segment_id WHERE e.start<? AND e.end>? ORDER BY e.start",
                (end + padding, start - padding),
            ):
                event = dict(row)
                event.update(
                    camera_id=store.settings.camera_id,
                    camera_name=store.settings.camera_name,
                )
                result.append(event)
    return sorted(result, key=lambda e: (e["start"], e["camera_id"], e["id"]))


def similarity(first, second, tolerance):
    """Compare overlapping normalised RMS envelopes allowing bounded RTSP skew."""
    a = np.asarray(json.loads(first.get("fingerprint") or "[]"), dtype=float)
    b = np.asarray(json.loads(second.get("fingerprint") or "[]"), dtype=float)
    if not len(a) or not len(b):
        return None
    best = 0.0
    for lag in np.arange(-tolerance, tolerance + 0.025, 0.05):
        begin = max(first["start"], second["start"] + lag)
        finish = min(first["end"], second["end"] + lag)
        if finish - begin < max(
            0.15,
            min(first["end"] - first["start"], second["end"] - second["start"]) * 0.5,
        ):
            continue
        grid = np.arange(begin, finish, 0.05)
        ai = np.minimum(((grid - first["start"]) / 0.05).astype(int), len(a) - 1)
        bi = np.clip(((grid - second["start"] - lag) / 0.05).astype(int), 0, len(b) - 1)
        x, y = a[ai], b[bi]
        x = x - x.mean()
        y = y - y.mean()
        denominator = float(np.linalg.norm(x) * np.linalg.norm(y))
        if denominator:
            best = max(best, float(x @ y / denominator))
    return best


def episodes(stores, settings, start, end):
    raw = source_events(stores, start, end, settings.dedup_seconds)
    accepted = [e for e in raw if e["review"] != "rejected"]
    groups = []
    for event in accepted:
        if (
            groups
            and event["start"]
            <= max(e["end"] for e in groups[-1]) + settings.episode_gap_seconds
        ):
            groups[-1].append(event)
        else:
            groups.append([event])
    result = []
    for members in groups:
        if (
            min(e["start"] for e in members) >= end
            or max(e["end"] for e in members) <= start
        ):
            continue
        parent = list(range(len(members)))

        def root(index):
            while parent[index] != index:
                index = parent[index]
            return index

        matches = []
        timing_only = False
        for i, first in enumerate(members):
            for j in range(i + 1, len(members)):
                second = members[j]
                if second["start"] - first["end"] > settings.dedup_seconds:
                    break
                if first["camera_id"] == second["camera_id"]:
                    continue
                score = similarity(first, second, settings.dedup_seconds)
                if score is None or score >= settings.dedup_similarity:
                    parent[root(j)] = root(i)
                    matches.append((i, j))
                    timing_only |= score is None
        clusters = {}
        for i, event in enumerate(members):
            clusters.setdefault(root(i), []).append(event)
        counts = []
        for cluster in clusters.values():
            if any(e["bark_count"] is None for e in cluster):
                counts.append(None)
                continue
            # Sum non-overlapping detections from a camera, then use the maximum
            # camera count for matched evidence. Never sum duplicate camera counts.
            camera_counts = {}
            for event in cluster:
                camera_counts[event["camera_id"]] = (
                    camera_counts.get(event["camera_id"], 0) + event["bark_count"]
                )
            counts.append(max(camera_counts.values()))
        measured = [e for e in members if e["rms_dbfs"] is not None]
        strongest = max(measured, key=lambda e: e["rms_dbfs"]) if measured else None
        first = max(start, min(e["start"] for e in members))
        last = min(end, max(e["end"] for e in members))
        count = (
            sum(counts)
            if all(c is not None for c in counts)
            and all(e["start"] >= start and e["end"] <= end for e in members)
            else None
        )
        identity = ",".join(
            sorted(f"{e['camera_id']}:{e['id']}:{e['segment_id']}" for e in members)
        )
        review = (
            "confirmed"
            if all(e["review"] == "confirmed" for e in members)
            else "unreviewed"
        )
        clips = [
            {
                "camera_id": e["camera_id"],
                "camera_name": e["camera_name"],
                "event_id": e["id"],
                "source_segment": e["segment_id"],
                "start": e["start"],
                "end": e["end"],
                "review": e["review"],
                "rms_dbfs": e["rms_dbfs"],
                "peak_dbfs": e["peak_dbfs"],
                "url": f"/api/clip/{e['id']}?camera={e['camera_id']}&segment={e['segment_id']}",
            }
            for e in members
        ]
        result.append(
            {
                "id": hashlib.sha256(identity.encode()).hexdigest()[:16],
                "start": first,
                "end": last,
                "duration": round(last - first, 3),
                "active_seconds": round(
                    union_seconds(
                        [(max(start, e["start"]), min(end, e["end"])) for e in members]
                    ),
                    3,
                ),
                "bark_count": count,
                "barks_per_minute": round(count * 60 / (last - first), 1)
                if count is not None and last > first
                else None,
                "rms_dbfs": strongest["rms_dbfs"] if strongest else None,
                "peak_dbfs": max(
                    (e["peak_dbfs"] for e in measured if e["peak_dbfs"] is not None),
                    default=None,
                ),
                "strongest_camera": strongest["camera_id"] if strongest else None,
                "strongest_camera_name": strongest["camera_name"]
                if strongest
                else None,
                "score": max(e["score"] for e in members),
                "review": review,
                "duplicate_detections": len(members) - len(clusters),
                "dedup_method": "timing-only (legacy metrics unavailable)"
                if timing_only
                else "time + audio envelope"
                if matches
                else "single/unmatched evidence",
                "clips": clips,
            }
        )
    return result, raw


def combined_summary(stores, settings, start, end, now):
    timeline, raw = episodes(stores, settings, start, end)
    elapsed_end = min(end, now)
    segments = []
    cameras = []
    for store in stores:
        with store.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM segments WHERE start<? AND start+duration>?",
                    (end, start),
                )
            ]
        intervals = [
            (max(start, r["start"]), min(elapsed_end, r["start"] + r["duration"]))
            for r in rows
            if r["start"] < elapsed_end
        ]
        analysed = [
            (max(start, r["start"]), min(elapsed_end, r["start"] + r["duration"]))
            for r in rows
            if r["start"] < elapsed_end and r["state"] == "done"
        ]
        cameras.append(
            {
                "camera_id": store.settings.camera_id,
                "camera_name": store.settings.camera_name,
                "recorded_seconds": union_seconds(intervals),
                "analysed_seconds": union_seconds(analysed),
            }
        )
        segments.extend(rows)
    elapsed = max(0, elapsed_end - start)
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
            if r["start"] < elapsed_end and r["state"] == "done"
        ]
    )
    active = union_seconds(
        [
            (max(start, e["start"]), min(end, e["end"]))
            for e in raw
            if e["review"] != "rejected"
        ]
    )
    estimated = (
        sum(e["bark_count"] for e in timeline)
        if all(e["bark_count"] is not None for e in timeline)
        else None
    )
    hourly = []
    cursor = start
    while cursor < end:
        finish = min(cursor + 3600, end)
        hourly.append(
            {
                "start": cursor,
                "seconds": union_seconds(
                    [
                        (max(cursor, e["start"]), min(finish, e["end"]))
                        for e in raw
                        if e["review"] != "rejected"
                        and e["start"] < finish
                        and e["end"] > cursor
                    ]
                ),
            }
        )
        cursor = finish
    return {
        "start": start,
        "end": end,
        "elapsed_seconds": elapsed,
        "episodes": len(timeline),
        "barking_seconds": round(active, 2),
        "longest_episode_seconds": max((e["duration"] for e in timeline), default=0),
        "estimated_barks": estimated,
        "barks_per_minute": round(estimated * 60 / analysed, 2)
        if estimated is not None and analysed
        else None,
        "episodes_per_hour": round(len(timeline) * 3600 / analysed, 2)
        if analysed
        else None,
        "peak_dbfs": max(
            (e["peak_dbfs"] for e in timeline if e["peak_dbfs"] is not None),
            default=None,
        ),
        "recorded_seconds": round(covered, 2),
        "analysed_seconds": round(analysed, 2),
        "coverage_percent": round(100 * covered / elapsed, 1) if elapsed else 0,
        "analysis_percent": round(100 * analysed / elapsed, 1) if elapsed else 0,
        "gap_seconds": round(max(0, elapsed - covered), 2),
        "pending_segments": sum(r["state"] == "pending" for r in segments),
        "failed_segments": sum(r["state"] == "failed" for r in segments),
        "duplicate_detections": sum(e["duplicate_detections"] for e in timeline),
        "reviewed": sum(e["review"] != "unreviewed" for e in raw),
        "cameras": cameras,
        "hourly": hourly,
        "legacy_episodes": sum("legacy" in e["dedup_method"] for e in timeline),
    }

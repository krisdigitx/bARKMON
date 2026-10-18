"""Readable evidence-review reports. Relative camera audio is not calibrated SPL."""

import io
from datetime import datetime
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    KeepTogether,
)

from barkmon.timeline import combined_summary, episodes


def render_pdf(stores, settings, start, end, now, mode, base_url=""):
    stats = combined_summary(stores, settings, start, end, now)
    timeline, _ = episodes(stores, settings, start, end)
    zone = ZoneInfo(settings.timezone)

    def stamp(value):
        return datetime.fromtimestamp(value, zone).strftime("%d %b %Y %H:%M:%S %Z")

    def number(value, suffix=""):
        return "Unavailable" if value is None else f"{value:.2f}{suffix}"

    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            "SmallNote",
            parent=styles["Normal"],
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#526256"),
        )
    )
    styles.add(ParagraphStyle("Cell", parent=styles["Normal"], fontSize=8, leading=11))
    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=(210 * mm, 297 * mm),
        leftMargin=17 * mm,
        rightMargin=17 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Dog Monitor - barking review report",
    )
    story = []

    def text(value, style="Normal"):
        story.append(Paragraph(escape(str(value)), styles[style]))

    def table(rows, widths=None):
        cells = [
            [Paragraph(escape(str(value)), styles["Cell"]) for value in row]
            for row in rows
        ]
        item = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
        item.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e4ebdf")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#ccd6c9")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.extend([item, Spacer(1, 5 * mm)])

    text("Dog Monitor", "Title")
    text(
        f"{'Nightly' if mode == 'night' else 'Weekly' if mode == 'week' else 'Daily'} barking activity review",
        "Heading2",
    )
    text(f"{stamp(start)} to {stamp(end)}")
    text(f"Generated {stamp(now)} | Time zone: {settings.timezone}", "SmallNote")
    text(
        "Cameras: "
        + ", ".join(
            f"{s.settings.camera_name} ({s.settings.camera_host})" for s in stores
        ),
        "SmallNote",
    )
    story.append(Spacer(1, 5 * mm))
    table(
        [
            ["Measure", "Result"],
            ["Combined activity episodes", stats["episodes"]],
            ["Estimated bark pulses (deduplicated)", number(stats["estimated_barks"])],
            ["Detected active barking", f"{stats['barking_seconds']:.2f} seconds"],
            ["Longest episode span", f"{stats['longest_episode_seconds']:.2f} seconds"],
            [
                "Estimated barking frequency",
                number(stats["barks_per_minute"], " pulses/min of analysed coverage"),
            ],
            [
                "Episode frequency",
                number(
                    stats["episodes_per_hour"], " episodes/hour of analysed coverage"
                ),
            ],
            [
                "Strongest digital peak",
                number(stats["peak_dbfs"], " dBFS (uncalibrated)"),
            ],
            ["Duplicate source detections grouped", stats["duplicate_detections"]],
            [
                "Any-camera recording / analysed coverage",
                f"{stats['coverage_percent']}% / {stats['analysis_percent']}% of elapsed period",
            ],
            ["Unrecorded elapsed time", f"{stats['gap_seconds']:.2f} seconds"],
            [
                "Pending / failed segments",
                f"{stats['pending_segments']} / {stats['failed_segments']}",
            ],
        ],
        [82 * mm, 94 * mm],
    )
    text("Coverage and review limits", "Heading2")
    table(
        [["Camera", "Recorded hours", "Analysed hours"]]
        + [
            [
                c["camera_name"],
                f"{c['recorded_seconds'] / 3600:.3f}",
                f"{c['analysed_seconds'] / 3600:.3f}",
            ]
            for c in stats["cameras"]
        ],
        [80 * mm, 48 * mm, 48 * mm],
    )
    for note in [
        "This report summarises retained recordings and manual reviews for complaint review. It does not determine nuisance or compliance.",
        "Relative RMS and peak levels are dBFS from decoded camera audio, not dB SPL or dBA. Camera sensitivity, placement and automatic gain affect comparisons. Strongest means strongest measured digital recording, not necessarily nearest to the dog.",
        f"Deduplication is a heuristic using timestamps within {settings.dedup_seconds}s and normalised audio-envelope similarity >= {settings.dedup_similarity}. Unmeasured legacy matches use timing only and are labelled. Simultaneous unrelated sounds or stream clock skew can affect matching; review source clips.",
        f"Positive activity within {settings.episode_gap_seconds}s forms an episode. Pulse counts and pulses/min are energy-based estimates gated by the barking classifier, not exact individual vocalisations or acoustic pitch.",
        "Rejected source detections are excluded. Missing/expired/unanalysed audio is not evidence of silence. Coverage is the union of cameras, never their sum. Rates use analysed coverage rather than missing time.",
        f"Audio is retained up to {settings.retention_days} days and may expire sooner at the storage cap. Download clips with this PDF to preserve them; clip links can expire. Reports represent the review state at generation time.",
    ]:
        text(note, "SmallNote")
        story.append(Spacer(1, 2 * mm))
    if mode == "week":
        text("Daily activity within the selected week", "Heading2")
        rows = [["Local date", "Episodes starting", "Active seconds"]]
        from datetime import timedelta

        cursor = datetime.fromtimestamp(start, zone)
        while cursor.timestamp() < end:
            finish = cursor + timedelta(days=1)
            day_stats = combined_summary(
                stores, settings, cursor.timestamp(), min(end, finish.timestamp()), now
            )
            rows.append(
                [
                    cursor.strftime("%d %b %Y"),
                    sum(
                        cursor.timestamp() <= e["start"] < finish.timestamp()
                        for e in timeline
                    ),
                    day_stats["barking_seconds"],
                ]
            )
            cursor = finish
        table(rows, [65 * mm, 55 * mm, 56 * mm])
    story.append(Spacer(1, 5 * mm) if mode == "week" else PageBreak())
    text("Combined event timeline", "Heading1")
    text(
        "Episodes are ordered by start time. Each includes its source evidence and strongest measured camera.",
        "SmallNote",
    )
    if not timeline:
        text(
            "No accepted barking episodes in the retained/analysed data for this period."
        )
    for index, event in enumerate(timeline, 1):
        block_start = len(story)
        text(f"Episode {index} | {event['id']}", "Heading3")
        text(f"Start: {stamp(event['start'])} | End: {stamp(event['end'])}")
        text(
            f"Span {event['duration']:.2f}s | Active {event['active_seconds']:.2f}s | Estimated pulses {number(event['bark_count'])} | {number(event['barks_per_minute'], ' pulses/min within episode')}"
        )
        text(
            f"Strongest measured source: {event['strongest_camera_name'] or 'Unavailable'} | RMS {number(event['rms_dbfs'], ' dBFS')} | Highest source peak {number(event['peak_dbfs'], ' dBFS')}"
        )
        text(
            f"Review: {event['review']} | Matching: {event['dedup_method']} | Duplicate detections grouped: {event['duplicate_detections']}",
            "SmallNote",
        )
        for clip in event["clips"]:
            label = f"{clip['camera_name']} / event {clip['event_id']} | {stamp(clip['start'])} to {stamp(clip['end'])} | RMS {number(clip['rms_dbfs'], ' dBFS')} | {clip['review']}"
            text(label, "SmallNote")
            if base_url:
                target = escape(base_url.rstrip("/") + clip["url"], {'"': "&quot;"})
                story.append(
                    Paragraph(
                        f'<link href="{target}" color="#38614d">Listen / download source clip</link>',
                        styles["SmallNote"],
                    )
                )
        story.append(Spacer(1, 4 * mm))
        block = story[block_start:]
        del story[block_start:]
        story.append(KeepTogether(block))

    def footer(canvas, document):
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#526256"))
        canvas.drawString(
            17 * mm,
            10 * mm,
            "Dog Monitor | relative audio estimates | retain original clips",
        )
        canvas.drawRightString(193 * mm, 10 * mm, str(document.page))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()

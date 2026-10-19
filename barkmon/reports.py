"""Readable evidence-review reports. Relative camera audio is not calibrated SPL."""

import io
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import reportlab
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
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
    Flowable,
)

from barkmon.timeline import combined_summary, episodes


# Embed the bundled open-source sans font for consistent PDF rendering.
_FONT_DIR = Path(reportlab.__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("ReportSans", str(_FONT_DIR / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("ReportSansBold", str(_FONT_DIR / "VeraBd.ttf")))
pdfmetrics.registerFontFamily(
    "ReportSans",
    normal="ReportSans",
    bold="ReportSansBold",
    italic="ReportSans",
    boldItalic="ReportSansBold",
)

# Match the dashboard's paper, ink and forest-green palette.
INK = colors.HexColor("#24362e")
MUTED = colors.HexColor("#6e7a70")
GREEN = colors.HexColor("#38614d")
PAPER = colors.HexColor("#f4f5ee")
LINE = colors.HexColor("#dce2d8")


class SummaryCards(Flowable):
    def __init__(self, cards):
        super().__init__()
        self.cards = cards
        self.width = 176 * mm
        self.height = 31 * mm

    def draw(self):
        canvas = self.canv
        gap = 3 * mm
        width = (self.width - 3 * gap) / 4
        for index, (label, value, detail) in enumerate(self.cards):
            x = index * (width + gap)
            canvas.setFillColor(colors.white)
            canvas.setStrokeColor(LINE)
            canvas.roundRect(x, 0, width, self.height, 3 * mm, fill=1, stroke=1)
            canvas.setFillColor(MUTED)
            canvas.setFont("ReportSans", 8)
            canvas.drawString(x + 4 * mm, 24 * mm, label)
            canvas.setFillColor(INK)
            canvas.setFont("Times-Roman", 25)
            canvas.drawString(x + 4 * mm, 12 * mm, value)
            canvas.setFillColor(MUTED)
            canvas.setFont("ReportSans", 7)
            canvas.drawString(x + 4 * mm, 5 * mm, detail)


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
    styles["Normal"].fontName = "ReportSans"
    styles["SmallNote"].fontName = "ReportSans"
    styles["Normal"].fontSize = 9
    styles["Normal"].leading = 13
    styles["Normal"].textColor = INK
    for name, size in [
        ("Title", 34),
        ("Heading1", 24),
        ("Heading2", 19),
        ("Heading3", 14),
    ]:
        styles[name].fontName = "Times-Roman"
        styles[name].fontSize = size
        styles[name].leading = size + 4
        styles[name].textColor = INK
        styles[name].alignment = 0
        styles[name].spaceBefore = 10
        styles[name].spaceAfter = 9
        styles[name].keepWithNext = True
    styles.add(ParagraphStyle("Cell", parent=styles["Normal"], fontSize=8, leading=11))
    styles.add(
        ParagraphStyle(
            "TableHeader",
            parent=styles["Cell"],
            textColor=colors.white,
            fontName="ReportSansBold",
        )
    )
    styles.add(
        ParagraphStyle(
            "Eyebrow",
            parent=styles["SmallNote"],
            textColor=GREEN,
            fontName="ReportSansBold",
            spaceAfter=6,
        )
    )
    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=(210 * mm, 297 * mm),
        leftMargin=17 * mm,
        rightMargin=17 * mm,
        topMargin=23 * mm,
        bottomMargin=20 * mm,
        title="Dog Monitor - barking review report",
    )
    story = []

    def text(value, style="Normal"):
        story.append(Paragraph(escape(str(value)), styles[style]))

    def table(rows, widths=None):
        cells = [
            [
                Paragraph(
                    escape(str(value)), styles["TableHeader" if index == 0 else "Cell"]
                )
                for value in row
            ]
            for index, row in enumerate(rows)
        ]
        item = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
        item.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), GREEN),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#eef1e8")],
                    ),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.extend([item, Spacer(1, 5 * mm)])

    text("BARKMON  /  ACTIVITY REPORT", "Eyebrow")
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
    story.extend(
        [
            SummaryCards(
                [
                    (
                        "Activity episodes",
                        str(stats["episodes"]),
                        "Combined camera timeline",
                    ),
                    (
                        "Active barking",
                        f"{stats['barking_seconds']:.0f}s",
                        "Detected positive activity",
                    ),
                    (
                        "Estimated pulses",
                        "N/A"
                        if stats["estimated_barks"] is None
                        else f"{stats['estimated_barks']:.0f}",
                        "Duplicate evidence grouped",
                    ),
                    (
                        "Analysed coverage",
                        f"{stats['analysis_percent']:.1f}%",
                        "Of the elapsed period",
                    ),
                ]
            ),
            Spacer(1, 5 * mm),
        ]
    )
    text("Activity at a glance", "Heading2")
    table(
        [
            ["Measure", "Result"],
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
    text("Camera coverage", "Heading2")
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
    if mode == "week":
        story.append(PageBreak())
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
    story.append(Spacer(1, 6 * mm) if mode == "week" else PageBreak())
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
        text(f"Episode {index:02d}  /  {event['review'].capitalize()}", "Heading3")
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
    story.append(PageBreak())
    text("Reading this report", "Heading1")
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

    def background(canvas, document):
        canvas.saveState()
        canvas.setFillColor(PAPER)
        canvas.rect(0, 0, 210 * mm, 297 * mm, fill=1, stroke=0)
        canvas.restoreState()

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(17 * mm, 281 * mm, 193 * mm, 281 * mm)
        canvas.line(17 * mm, 16 * mm, 193 * mm, 16 * mm)
        canvas.setFont("ReportSansBold", 8)
        canvas.setFillColor(GREEN)
        canvas.drawString(17 * mm, 285 * mm, "DOG MONITOR")
        canvas.setFont("ReportSans", 8)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(
            193 * mm,
            285 * mm,
            f"{mode.upper()} / {datetime.fromtimestamp(start, zone):%d %b %Y}",
        )
        canvas.drawString(
            17 * mm,
            10 * mm,
            "Relative audio estimates  |  Retain original source clips",
        )
        canvas.drawRightString(193 * mm, 10 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.afterPage = lambda: footer(doc.canv, doc)
    doc.build(story, onFirstPage=background, onLaterPages=background)
    return output.getvalue()

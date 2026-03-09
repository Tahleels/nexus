"""
artifact_builder.py — Nexus AI · Jobs & Scheduler

Generates PDF bytes for dashboard / report / infographic artifacts
using reportlab (pure Python, no browser required).

Public API:
    build_dashboard_pdf(config, raw_data)    -> bytes
    build_report_pdf(report_config, raw_data) -> bytes
    build_infographic_pdf(infographic)        -> bytes
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional
from generator_utils import clean_nan as _clean_nan

# ── reportlab imports ──────────────────────────────────────────────────────────
from reportlab.lib                    import colors
from reportlab.lib.pagesizes          import A4, landscape
from reportlab.lib.units              import mm, cm
from reportlab.lib.styles             import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums              import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus               import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether, PageBreak,
)
from reportlab.graphics.shapes        import Drawing, Rect, String, Line
from reportlab.graphics.charts.barcharts  import VerticalBarChart
from reportlab.graphics.charts.piecharts  import Pie
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics               import renderPDF


# ── Colour palette ─────────────────────────────────────────────────────────────
BRAND_DARK   = colors.HexColor("#2c3e50")
BRAND_BLUE   = colors.HexColor("#3498db")
BRAND_PURPLE = colors.HexColor("#6366f1")
BRAND_GREEN  = colors.HexColor("#10b981")
BRAND_AMBER  = colors.HexColor("#f59e0b")
BRAND_RED    = colors.HexColor("#ef4444")
GREY_LIGHT   = colors.HexColor("#f3f4f6")
GREY_MID     = colors.HexColor("#e5e7eb")
GREY_TEXT    = colors.HexColor("#6b7280")
WHITE        = colors.white
BLACK        = colors.black

CHART_PALETTE = [
    colors.HexColor("#3498db"), colors.HexColor("#e74c3c"),
    colors.HexColor("#2ecc71"), colors.HexColor("#f39c12"),
    colors.HexColor("#9b59b6"), colors.HexColor("#1abc9c"),
    colors.HexColor("#34495e"), colors.HexColor("#e67e22"),
]


def _pal(n: int) -> list:
    """Returns the first `n` colors from CHART_PALETTE, cycling if `n` exceeds its length."""
    return [CHART_PALETTE[i % len(CHART_PALETTE)] for i in range(n)]


def _fmt_num(val: Any, prefix: str = "") -> str:
    """Formats a numeric value for display, abbreviating large magnitudes.

    Args:
        val: Value to format; coerced to float. Non-numeric values fall back
            to their string form (or "—" if None).
        prefix: If "$", formats as currency with 2 decimals (e.g. "$1,234.56").
            Otherwise large values are abbreviated with K/M suffixes
            (e.g. 1_500 -> "1.5K", 2_000_000 -> "2.0M").

    Returns:
        The formatted string, or the stringified/`"—"` fallback on failure.
    """
    try:
        f = float(val)
        if prefix == "$":
            return f"${f:,.2f}"
        if f >= 1_000_000:
            return f"{f/1_000_000:.1f}M"
        if f >= 1_000:
            return f"{f/1_000:.1f}K"
        return f"{f:,.0f}"
    except (TypeError, ValueError):
        return str(val) if val is not None else "—"


# ── Shared styles ──────────────────────────────────────────────────────────────

def _base_styles():
    """Builds the reportlab ParagraphStyle sheet shared by all three PDF builders.

    Extends reportlab's default sample stylesheet with the brand-specific
    styles used throughout dashboard/report/infographic PDFs (headings, KPI
    value/label text, chart titles, table header/cell text, muted captions).

    Returns:
        A reportlab `StyleSheet1` with the extra named styles added.
    """
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("BrandH1", parent=styles["Heading1"],
        fontSize=20, textColor=WHITE, spaceAfter=4, leading=24))
    styles.add(ParagraphStyle("BrandH2", parent=styles["Heading2"],
        fontSize=13, textColor=BRAND_DARK, spaceAfter=6, leading=16))
    styles.add(ParagraphStyle("BrandSub", parent=styles["Normal"],
        fontSize=10, textColor=WHITE, spaceAfter=0))
    styles.add(ParagraphStyle("KpiVal", parent=styles["Normal"],
        fontSize=22, textColor=BRAND_DARK, fontName="Helvetica-Bold",
        alignment=TA_CENTER, leading=26))
    styles.add(ParagraphStyle("KpiLbl", parent=styles["Normal"],
        fontSize=8, textColor=GREY_TEXT, alignment=TA_CENTER,
        spaceAfter=0, leading=10))
    styles.add(ParagraphStyle("ChartTitle", parent=styles["Normal"],
        fontSize=10, textColor=BRAND_DARK, fontName="Helvetica-Bold",
        alignment=TA_CENTER, spaceAfter=4))
    styles.add(ParagraphStyle("BodySmall", parent=styles["Normal"],
        fontSize=9, textColor=BRAND_DARK, leading=13))
    styles.add(ParagraphStyle("BulletItem", parent=styles["Normal"],
        fontSize=9, textColor=BRAND_DARK, leading=14, leftIndent=12,
        bulletIndent=4))
    styles.add(ParagraphStyle("SectionTitle", parent=styles["Normal"],
        fontSize=11, textColor=BRAND_DARK, fontName="Helvetica-Bold",
        spaceAfter=6, spaceBefore=10, leading=14))
    styles.add(ParagraphStyle("TableHeader", parent=styles["Normal"],
        fontSize=9, textColor=WHITE, fontName="Helvetica-Bold",
        alignment=TA_CENTER, leading=11))
    styles.add(ParagraphStyle("TableCell", parent=styles["Normal"],
        fontSize=8, textColor=BRAND_DARK, leading=10))
    styles.add(ParagraphStyle("Muted", parent=styles["Normal"],
        fontSize=8, textColor=GREY_TEXT, leading=10))
    return styles


# ── Header banner ──────────────────────────────────────────────────────────────

def _header_flowable(title: str, subtitle: str,
                     accent: Any = BRAND_BLUE,
                     page_width: float = A4[0]) -> Table:
    """A dark gradient-style header band."""
    styles = _base_styles()
    inner = Table(
        [[Paragraph(title,    styles["BrandH1"]),
          Paragraph(subtitle, styles["BrandSub"])]],
        colWidths=[page_width * 0.65, page_width * 0.35],
    )
    inner.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, -1), BRAND_DARK),
        ("LEFTPADDING",  (0, 0), (-1, -1), 18),
        ("RIGHTPADDING", (0, 0), (-1, -1), 18),
        ("TOPPADDING",   (0, 0), (-1, -1), 16),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 16),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",        (1, 0), (1, 0),   "RIGHT"),
    ]))
    return inner


# ── Bar chart drawing ──────────────────────────────────────────────────────────

def _bar_chart(labels: List[str], values: List[float],
               title: str = "", width: float = 200, height: float = 130) -> Drawing:
    """Renders a single-series vertical bar chart as a reportlab Drawing.

    Args:
        labels: Category names (truncated to 14 chars, shown at a 30° angle).
        values: One numeric value per label.
        title: Optional title rendered above the chart.
        width: Drawing width in points.
        height: Drawing height in points.

    Returns:
        A reportlab `Drawing` flowable ready to be appended to a PDF story.
    """
    d = Drawing(width, height)
    # clip labels
    short_labels = [str(l)[:14] for l in labels]
    bc = VerticalBarChart()
    bc.x = 30; bc.y = 30
    bc.width  = width  - 40
    bc.height = height - 50
    bc.data   = [values]
    bc.categoryAxis.categoryNames = short_labels
    bc.categoryAxis.labels.angle  = 30
    bc.categoryAxis.labels.fontSize = 7
    bc.categoryAxis.labels.dy      = -8
    bc.valueAxis.labels.fontSize   = 7
    bc.valueAxis.forceZero         = True
    bc.bars[0].fillColor           = BRAND_BLUE
    bc.bars[0].strokeColor         = WHITE
    bc.bars[0].strokeWidth         = 0.5
    if title:
        d.add(String(width / 2, height - 12, title,
                     textAnchor="middle", fontSize=9,
                     fontName="Helvetica-Bold", fillColor=BRAND_DARK))
    d.add(bc)
    return d


def _pie_chart(labels: List[str], values: List[float],
               title: str = "", width: float = 180, height: float = 150) -> Drawing:
    """Renders a pie chart (used for both "pie" and "doughnut" chart types) as a reportlab Drawing.

    Slices are colored from `CHART_PALETTE` via `_pal()` and labeled with
    side labels (no visual distinction is drawn between pie and doughnut —
    callers map both chart types to this function).

    Args:
        labels: Slice names (truncated to 12 chars).
        values: One numeric value per slice.
        title: Optional title rendered above the chart.
        width: Drawing width in points.
        height: Drawing height in points.

    Returns:
        A reportlab `Drawing` flowable ready to be appended to a PDF story.
    """
    d = Drawing(width, height)
    pie = Pie()
    pie.x      = 30; pie.y = 20
    pie.width  = 100; pie.height = 100
    pie.data   = values
    pie.labels = [str(l)[:12] for l in labels]
    pie.sideLabels      = True
    pie.sideLabelsOffset= 0.05
    pie.slices.fontSize = 7
    for i, c in enumerate(_pal(len(values))):
        pie.slices[i].fillColor   = c
        pie.slices[i].strokeColor = WHITE
        pie.slices[i].strokeWidth = 0.8
    if title:
        d.add(String(width / 2, height - 10, title,
                     textAnchor="middle", fontSize=9,
                     fontName="Helvetica-Bold", fillColor=BRAND_DARK))
    d.add(pie)
    return d


def _line_chart(labels: List[str], values: List[float],
                title: str = "", width: float = 200, height: float = 130) -> Drawing:
    """Renders a single-series line chart as a reportlab Drawing.

    Args:
        labels: Category names (truncated to 10 chars, shown at a 30° angle).
        values: One numeric value per label.
        title: Optional title rendered above the chart.
        width: Drawing width in points.
        height: Drawing height in points.

    Returns:
        A reportlab `Drawing` flowable ready to be appended to a PDF story.
    """
    d = Drawing(width, height)
    lc = HorizontalLineChart()
    lc.x = 30; lc.y = 30
    lc.width  = width  - 40
    lc.height = height - 50
    lc.data   = [values]
    lc.categoryAxis.categoryNames = [str(l)[:10] for l in labels]
    lc.categoryAxis.labels.angle  = 30
    lc.categoryAxis.labels.fontSize = 7
    lc.categoryAxis.labels.dy      = -8
    lc.valueAxis.labels.fontSize   = 7
    lc.valueAxis.forceZero         = True
    lc.lines[0].strokeColor = BRAND_BLUE
    lc.lines[0].strokeWidth = 2
    if title:
        d.add(String(width / 2, height - 12, title,
                     textAnchor="middle", fontSize=9,
                     fontName="Helvetica-Bold", fillColor=BRAND_DARK))
    d.add(lc)
    return d


def _build_chart_drawing(chart_type: str, labels: List[str],
                          values: List[float], title: str,
                          width: float = 220, height: float = 150) -> Optional[Drawing]:
    """Dispatches to the matching reportlab chart-drawing function by chart type.

    This is the PDF-rendering counterpart of the chart-type selection done
    upstream by the LLM (in `dashboard_generator.py` / `infographicgenerator.py`):
    those modules decide *which* chart type to use for a given column pair,
    and this function turns that decision into an actual reportlab Drawing.

    Args:
        chart_type: Chart type string, case-insensitive. "bar" -> bar chart;
            "doughnut" or "pie" -> pie chart; "line" -> line chart; any other
            value (e.g. "bubble", "radar", unrecognized strings) silently
            falls back to a bar chart.
        labels: Category/slice labels.
        values: One numeric value per label.
        title: Optional chart title.
        width: Drawing width in points.
        height: Drawing height in points.

    Returns:
        A reportlab `Drawing`, or None if `labels` or `values` is empty.
    """
    if not labels or not values:
        return None
    ct = chart_type.lower()
    if ct in ("bar",):
        return _bar_chart(labels, values, title, width, height)
    elif ct in ("doughnut", "pie"):
        return _pie_chart(labels, values, title, width, height)
    elif ct in ("line",):
        return _line_chart(labels, values, title, width, height)
    else:
        return _bar_chart(labels, values, title, width, height)


# ── KPI card strip ─────────────────────────────────────────────────────────────

def _kpi_table(kpi_items: List[Dict], page_width: float) -> Table:
    """Renders up to 4 KPI cards in a horizontal strip.

    Args:
        kpi_items: List of `{"label": str, "value": str}` dicts. Only the
            first 4 are rendered; extras are silently dropped.
        page_width: Available width in points, split evenly across columns.

    Returns:
        A reportlab `Table` flowable with two rows (value, label) per KPI.
    """
    styles = _base_styles()
    n      = min(len(kpi_items), 4)
    col_w  = (page_width - 40) / n

    cells = []
    for item in kpi_items[:n]:
        cells.append([
            Paragraph(item["value"], styles["KpiVal"]),
            Paragraph(item["label"], styles["KpiLbl"]),
        ])

    tdata  = [[c[0] for c in cells],
              [c[1] for c in cells]]
    widths = [col_w] * n

    t = Table(tdata, colWidths=widths)
    style = [
        ("BACKGROUND",   (0, 0), (-1, -1), GREY_LIGHT),
        ("ROWBACKGROUND",(0, 0), (-1,  0), WHITE),
        ("TOPPADDING",   (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 8),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("ALIGN",        (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW",    (0, 0), (-1,  0), 3, BRAND_BLUE),
        ("BOX",          (0, 0), (-1, -1), 0.5, GREY_MID),
        ("INNERGRID",    (0, 0), (-1, -1), 0.5, GREY_MID),
        ("ROUNDEDCORNERS", [4]),
    ]
    t.setStyle(TableStyle(style))
    return t


# ════════════════════════════════════════════════════════════════
# DASHBOARD PDF
# ════════════════════════════════════════════════════════════════

def build_dashboard_pdf(config: Dict, raw_data: List[Dict]) -> bytes:
    """Renders a dashboard config + raw rows into a landscape A4 PDF.

    Builds, in order: a branded header banner, up to 4 KPI cards (aggregated
    from `raw_data` per `config["kpi_cards"]`'s calculation/column), and the
    charts in `config["charts"]` (aggregated/sorted from `raw_data` and
    rendered 2-per-row via `_build_chart_drawing`). This is the PDF
    equivalent of the JSON dashboard config produced by
    `dashboard_generator.generate_dashboard_config()`.

    Args:
        config: Dashboard config dict shaped like
            `dashboard_generator.get_dashboard_json_structure()` — keys
            `header`, `kpi_cards`, `charts` (each chart has `type`,
            `title`, `data.label_column`, `data.value_column`).
        raw_data: BI query result rows (list of column-name-keyed dicts)
            used to compute KPI values and chart series.

    Returns:
        The rendered PDF as raw bytes (suitable for an email attachment or
        `send_file`).
    """
    config   = _clean_nan(config)
    raw_data = _clean_nan(raw_data)

    buf      = io.BytesIO()
    page_w, page_h = landscape(A4)
    doc = SimpleDocTemplate(buf,
        pagesize=landscape(A4),
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=14*mm,  bottomMargin=14*mm,
    )
    styles  = _base_styles()
    story   = []
    usable_w = page_w - 36*mm

    # ── Header ─────────────────────────────────────────────────
    hdr = config.get("header", {})
    story.append(_header_flowable(
        hdr.get("title",    "Dashboard"),
        hdr.get("subtitle", ""),
        page_width=usable_w,
    ))
    story.append(Spacer(1, 10))

    # ── KPI cards ───────────────────────────────────────────────
    kpi_cards = config.get("kpi_cards", [])
    if kpi_cards and raw_data:
        kpi_items = []
        for kpi in kpi_cards[:4]:
            col  = kpi.get("column_name", "")
            calc = kpi.get("calculation", "COUNT")
            pfx  = kpi.get("prefix", "")
            vals = [row.get(col) for row in raw_data if row.get(col) is not None]
            try:
                nums = [float(v) for v in vals]
                if calc == "SUM":            result = sum(nums)
                elif calc == "AVERAGE":      result = sum(nums) / len(nums) if nums else 0
                elif calc == "COUNT":        result = len(vals)
                elif calc == "COUNT_DISTINCT": result = len(set(vals))
                else:                        result = len(vals)
            except (TypeError, ValueError):
                result = len(vals)
            kpi_items.append({
                "label": kpi.get("label", col),
                "value": _fmt_num(result, pfx),
            })
        story.append(_kpi_table(kpi_items, usable_w))
        story.append(Spacer(1, 12))

    # ── Charts (2 per row) ──────────────────────────────────────
    charts = config.get("charts", [])
    chart_w = (usable_w - 20) / 2
    chart_h = 160

    chart_drawings = []
    for ch in charts:
        cdata  = ch.get("data", {})
        lcol   = cdata.get("label_column",  "")
        vcol   = cdata.get("value_column",  "")
        ctype  = ch.get("type", "bar")
        ctitle = ch.get("title", "")

        agg: Dict[str, float] = {}
        for row in raw_data:
            lbl = row.get(lcol)
            try:
                v = float(row.get(vcol) or 0)
            except (TypeError, ValueError):
                continue
            if lbl is not None:
                agg[str(lbl)] = agg.get(str(lbl), 0) + v

        if not agg:
            continue

        sorted_items = sorted(agg.items(), key=lambda x: x[1], reverse=True)
        if ctype != "line":
            sorted_items = sorted_items[:8]
        else:
            sorted_items = sorted(sorted_items, key=lambda x: x[0])

        labels = [i[0] for i in sorted_items]
        values = [i[1] for i in sorted_items]
        drw = _build_chart_drawing(ctype, labels, values, ctitle,
                                   width=chart_w, height=chart_h)
        if drw:
            chart_drawings.append(drw)

    # pair up charts into rows of 2
    for i in range(0, len(chart_drawings), 2):
        pair = chart_drawings[i:i+2]
        if len(pair) == 2:
            row_table = Table([[pair[0], pair[1]]],
                              colWidths=[chart_w, chart_w])
            row_table.setStyle(TableStyle([
                ("ALIGN",   (0, 0), (-1, -1), "CENTER"),
                ("VALIGN",  (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING",  (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("BOX",     (0, 0), (-1, -1), 0.5, GREY_MID),
            ]))
        else:
            row_table = Table([[pair[0]]],
                              colWidths=[chart_w])
            row_table.setStyle(TableStyle([
                ("ALIGN",  (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
        story.append(row_table)
        story.append(Spacer(1, 8))

    doc.build(story)
    return buf.getvalue()


# ════════════════════════════════════════════════════════════════
# REPORT PDF
# ════════════════════════════════════════════════════════════════

def build_report_pdf(report_config: Dict, raw_data: List[Dict]) -> bytes:
    """Renders a tabular SSRS-style report into a landscape A4 PDF.

    Builds a branded header followed by a single data table covering every
    column present in the first row of `raw_data` (column widths divided
    evenly, clamped between 18mm and 55mm). Only the first 500 rows are
    rendered, with a footnote if more were supplied. This is the PDF
    equivalent of the JSON config produced by
    `reportgenerator.generate_report_config()`.

    Args:
        report_config: Report config dict; only `header.title` /
            `header.subtitle` are used here (filters/kpis/tables produced
            by `reportgenerator` are not rendered in this PDF path).
        raw_data: BI query result rows (list of column-name-keyed dicts).
            If empty, a "No data available." placeholder PDF is returned.

    Returns:
        The rendered PDF as raw bytes.
    """
    raw_data = _clean_nan(raw_data)
    header   = (report_config or {}).get("header", {})

    buf = io.BytesIO()
    page_w, page_h = landscape(A4)
    doc = SimpleDocTemplate(buf,
        pagesize=landscape(A4),
        leftMargin=14*mm, rightMargin=14*mm,
        topMargin=12*mm,  bottomMargin=12*mm,
    )
    styles   = _base_styles()
    story    = []
    usable_w = page_w - 28*mm

    # ── Header ─────────────────────────────────────────────────
    story.append(_header_flowable(
        header.get("title",    "Report"),
        header.get("subtitle", f"{len(raw_data)} rows"),
        page_width=usable_w,
    ))
    story.append(Spacer(1, 10))

    if not raw_data:
        story.append(Paragraph("No data available.", styles["BodySmall"]))
        doc.build(story)
        return buf.getvalue()

    columns = list(raw_data[0].keys())

    # ── Determine column widths ─────────────────────────────────
    n_cols   = len(columns)
    min_col  = 18 * mm
    max_col  = 55 * mm
    col_w    = max(min_col, min(max_col, usable_w / n_cols))
    # if total exceeds usable_w, shrink uniformly
    total    = col_w * n_cols
    if total > usable_w:
        col_w = usable_w / n_cols
    col_widths = [col_w] * n_cols

    # ── Table header row ────────────────────────────────────────
    header_row = [Paragraph(str(c)[:20], styles["TableHeader"]) for c in columns]
    table_data = [header_row]

    # ── Data rows (max 500 for PDF manageability) ───────────────
    for row in raw_data[:500]:
        cells = []
        for col in columns:
            val = row.get(col)
            txt = "" if val is None else str(val)
            if len(txt) > 40:
                txt = txt[:37] + "…"
            cells.append(Paragraph(txt, styles["TableCell"]))
        table_data.append(cells)

    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    ts = TableStyle([
        # Header
        ("BACKGROUND",   (0, 0), (-1, 0),  BRAND_DARK),
        ("TEXTCOLOR",    (0, 0), (-1, 0),  WHITE),
        ("FONTNAME",     (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0),  8),
        ("ALIGN",        (0, 0), (-1, 0),  "CENTER"),
        ("TOPPADDING",   (0, 0), (-1, 0),  7),
        ("BOTTOMPADDING",(0, 0), (-1, 0),  7),
        # Body
        ("FONTSIZE",     (0, 1), (-1, -1), 7.5),
        ("TOPPADDING",   (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 1), (-1, -1), 4),
        ("LEFTPADDING",  (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, GREY_LIGHT]),
        ("GRID",         (0, 0), (-1, -1), 0.4, GREY_MID),
        ("LINEBELOW",    (0, 0), (-1,  0), 1.5, BRAND_BLUE),
    ])
    t.setStyle(ts)
    story.append(t)

    if len(raw_data) > 500:
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            f"* Showing first 500 of {len(raw_data)} rows.",
            styles["Muted"],
        ))

    doc.build(story)
    return buf.getvalue()


# ════════════════════════════════════════════════════════════════
# INFOGRAPHIC PDF
# ════════════════════════════════════════════════════════════════

def build_infographic_pdf(infographic: Dict) -> bytes:
    """Renders an infographic widget plan into a portrait A4 PDF.

    Iterates `infographic["widgets"]` and renders each by `widget_type`:
    "summary" as a bulleted key-insights block, "kpi_showcase" as a KPI
    card strip (via `_kpi_table`), "breakdown_list" as a two-column ranked
    table, and "chart" as a chart drawing (accumulated and flushed 2-per-row
    at the end). This is the PDF equivalent of the JSON layout produced by
    `infographicgenerator.InfographicGenerator.generate_infographic_layout()`.

    Args:
        infographic: Infographic dict with `title`, `subtitle`, and
            `widgets` (list of widget dicts, each with `widget_type`/`type`
            and widget-specific fields as built by
            `infographicgenerator.py`'s `_build_*_widget()` helpers).

    Returns:
        The rendered PDF as raw bytes.
    """
    infographic = _clean_nan(infographic)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf,
        pagesize=A4,
        leftMargin=16*mm, rightMargin=16*mm,
        topMargin=14*mm,  bottomMargin=14*mm,
    )
    styles   = _base_styles()
    story    = []
    page_w   = A4[0]
    usable_w = page_w - 32*mm

    # ── Header ─────────────────────────────────────────────────
    story.append(_header_flowable(
        infographic.get("title",    "Infographic"),
        infographic.get("subtitle", ""),
        accent=BRAND_PURPLE,
        page_width=usable_w,
    ))
    story.append(Spacer(1, 10))

    chart_drawings = []

    for widget in infographic.get("widgets", []):
        wtype = widget.get("widget_type") or widget.get("type", "")

        # ── Summary / key insights ──────────────────────────────
        if wtype == "summary":
            story.append(Paragraph("📌 Key Insights", styles["SectionTitle"]))
            for pt in (widget.get("points") or []):
                story.append(Paragraph(f"• {pt}", styles["BulletItem"]))
            story.append(Spacer(1, 8))

        # ── KPI showcase ────────────────────────────────────────
        elif wtype == "kpi_showcase":
            items = widget.get("data", [])
            if items:
                n      = min(len(items), 4)
                col_w  = usable_w / n
                kpi_items = [{"label": it.get("label",""), "value": str(it.get("value",""))}
                             for it in items[:n]]
                story.append(_kpi_table(kpi_items, usable_w))
                story.append(Spacer(1, 10))

        # ── Breakdown list ──────────────────────────────────────
        elif wtype == "breakdown_list":
            title = widget.get("title", "Breakdown")
            story.append(Paragraph(title, styles["SectionTitle"]))
            items = widget.get("data", [])
            if items:
                tdata = [[
                    Paragraph(str(it.get("label", "")), styles["BodySmall"]),
                    Paragraph(str(it.get("value", "")), styles["BodySmall"]),
                ] for it in items]
                bt = Table(tdata, colWidths=[usable_w * 0.65, usable_w * 0.35])
                bt.setStyle(TableStyle([
                    ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, GREY_LIGHT]),
                    ("GRID",          (0, 0), (-1, -1), 0.4, GREY_MID),
                    ("TOPPADDING",    (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
                    ("ALIGN",         (1, 0), (1, -1), "RIGHT"),
                    ("FONTNAME",      (1, 0), (1, -1), "Helvetica-Bold"),
                    ("TEXTCOLOR",     (1, 0), (1, -1), BRAND_PURPLE),
                    ("LINEBELOW",     (0, -1), (-1, -1), 1, BRAND_PURPLE),
                ]))
                story.append(bt)
                story.append(Spacer(1, 10))

        # ── Chart ───────────────────────────────────────────────
        elif wtype == "chart":
            ctype  = widget.get("chart_type", "bar")
            ctitle = widget.get("title", "")
            cdata  = widget.get("data", {})
            labels = cdata.get("labels", [])
            # handle single or multi-dataset
            raw_datasets = cdata.get("datasets", [])
            if raw_datasets:
                values = [float(v) for v in (raw_datasets[0].get("data") or [])
                          if v is not None]
            else:
                values = []
            if labels and values:
                drw = _build_chart_drawing(ctype, labels, values, ctitle,
                                           width=usable_w * 0.9, height=170)
                if drw:
                    chart_drawings.append(drw)

    # ── Flush accumulated charts (2-up) ────────────────────────
    if chart_drawings:
        story.append(Paragraph("Charts", styles["SectionTitle"]))
        cw = (usable_w - 10) / 2
        ch = 180
        for i in range(0, len(chart_drawings), 2):
            pair = chart_drawings[i:i+2]
            if len(pair) == 2:
                row_tbl = Table([[pair[0], pair[1]]], colWidths=[cw, cw])
            else:
                row_tbl = Table([[pair[0]]], colWidths=[usable_w * 0.9])
            row_tbl.setStyle(TableStyle([
                ("ALIGN",  (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX",    (0, 0), (-1, -1), 0.4, GREY_MID),
            ]))
            story.append(row_tbl)
            story.append(Spacer(1, 8))

    doc.build(story)
    return buf.getvalue()
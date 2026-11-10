"""
Builds the downloadable PDF for the Category Sales page.

Separate from category_sales_page.py so the PDF layout can be tweaked
without touching the page logic itself (same pattern as pace_pdf_report.py
and touch_tell_pdf.py).

Layout:
  - Title + what was searched (category / keyword / UPC), date range,
    and when the report was generated
  - Summary: Total Sales, Total Units Sold, number of stores
  - Store table (in whatever order the table is currently sorted on the
    page) with a bold totals row
  - Item breakdown: one small table per store listing every matching item
    sold, alphabetically, with that store's subtotal. Stores with no
    matching sales are listed together at the end instead of getting an
    empty table each.
"""
import io
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

HEADER_BG = colors.HexColor("#2f3542")
STRIPE_BG = colors.HexColor("#f1f2f6")
TOTALS_BG = colors.HexColor("#dfe4ea")
GRID = colors.HexColor("#ced6e0")
MUTED = colors.HexColor("#57606f")

_styles = getSampleStyleSheet()
_title = ParagraphStyle("CSTitle", parent=_styles["Title"], fontSize=18, spaceAfter=4)
_meta = ParagraphStyle("CSMeta", parent=_styles["Normal"], fontSize=9.5, leading=13, textColor=MUTED)
_h2 = ParagraphStyle("CSH2", parent=_styles["Heading2"], fontSize=12.5, spaceBefore=8, spaceAfter=6)
_h3 = ParagraphStyle("CSH3", parent=_styles["Heading3"], fontSize=10.5, spaceBefore=6, spaceAfter=4)
_cell = ParagraphStyle("CSCell", parent=_styles["Normal"], fontName="Helvetica", fontSize=8.5, leading=10.5)
_cell_bold = ParagraphStyle("CSCellBold", parent=_cell, fontName="Helvetica-Bold")
_head = ParagraphStyle("CSHead", parent=_cell, fontName="Helvetica-Bold", textColor=colors.white)
_card_label = ParagraphStyle("CSCardLabel", parent=_cell, fontSize=7.5, textColor=MUTED, fontName="Helvetica-Bold")
_card_value = ParagraphStyle("CSCardValue", parent=_cell, fontSize=14, leading=17, fontName="Helvetica-Bold")


def _p(text, style=_cell):
    return Paragraph(escape(str(text)), style)


def _money(value):
    return f"${value:,.2f}"


def _units(value):
    # Units are almost always whole numbers; show a decimal only when not.
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _table_style(has_totals_row, num_rows):
    cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE_BG]),
        ("GRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if has_totals_row:
        last = num_rows - 1
        cmds.append(("BACKGROUND", (0, last), (-1, last), TOTALS_BG))
    return TableStyle(cmds)


def _right(style):
    return ParagraphStyle(style.name + "Right", parent=style, alignment=2)


def describe_search(report):
    """Human-readable description of what was searched, e.g.
    'Category: Rolling Trays' or 'Keyword: mama (excluding: pacha)'."""
    mode = report["search_mode"]
    if mode == "Category":
        return f"Category: {report.get('selected_category') or ''}"
    if mode == "Merged Category":
        members = report.get("merged_members") or []
        return (
            f"Merged category: {report.get('selected_category') or ''}"
            f" (includes: {', '.join(members)})"
        )
    if mode == "Keyword":
        text = f"Keyword: {report.get('keyword') or ''}"
        if report.get("exclude_keyword"):
            text += f" (excluding: {report['exclude_keyword']})"
        return text
    return f"UPC: {report.get('upc') or ''}"


def build_category_sales_pdf(report, rows):
    """
    report: the cat_sales_report dict from session state
    rows: list of row dicts as built in render_report (store_name, total,
          quantity, items), already sorted the way the table shows them
    Returns a BytesIO ready for st.download_button.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        title="Category Sales Report", author="WOSV Dashboard",
    )
    usable = letter[0] - doc.leftMargin - doc.rightMargin

    start, end = report["start_date"], report["end_date"]
    if start == end:
        period = start.strftime("%b %d, %Y")
    else:
        period = f"{start.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}"

    total_sales = sum(r["total"] for r in rows)
    total_units = sum(r["quantity"] for r in rows)

    story = [
        Paragraph("Category Sales Report", _title),
        Paragraph(f"<b>{escape(describe_search(report))}</b>", _meta),
        Paragraph(f"Period: {escape(period)}", _meta),
        Paragraph(f"Generated {datetime.now().strftime('%b %d, %Y %I:%M %p')}", _meta),
        Spacer(1, 12),
    ]

    # Summary cards
    store_count = f"{len(rows)} store{'s' if len(rows) != 1 else ''}"
    cards = Table(
        [[
            [_p("TOTAL SALES", _card_label), _p(_money(total_sales), _card_value)],
            [_p("TOTAL UNITS SOLD", _card_label), _p(_units(total_units), _card_value)],
            [_p("STORES", _card_label), _p(store_count, _card_value)],
        ]],
        colWidths=[usable / 3] * 3,
    )
    cards.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, GRID),
        ("LINEBEFORE", (1, 0), (-1, 0), 0.6, GRID),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story += [cards, Spacer(1, 14)]

    # Store table
    story.append(Paragraph("Sales by Store", _h2))
    right_head, right_cell, right_bold = _right(_head), _right(_cell), _right(_cell_bold)
    data = [[_p("Store", _head), _p("Total Sales", right_head),
             _p("Units Sold", right_head), _p("Items", right_head)]]
    for r in rows:
        data.append([
            _p(r["store_name"]),
            _p(_money(r["total"]), right_cell),
            _p(_units(r["quantity"]), right_cell),
            _p(r["item_count"], right_cell),
        ])
    data.append([
        _p("Total", _cell_bold),
        _p(_money(total_sales), right_bold),
        _p(_units(total_units), right_bold),
        _p("", right_bold),
    ])
    store_table = Table(data, colWidths=[usable * 0.46, usable * 0.2, usable * 0.18, usable * 0.16], repeatRows=1)
    store_table.setStyle(_table_style(True, len(data)))
    story.append(store_table)

    # Item breakdown per store
    with_items = [r for r in rows if r["items"]]
    without_items = [r["store_name"] for r in rows if not r["items"]]

    if with_items:
        story += [Spacer(1, 10), Paragraph("Item Breakdown by Store", _h2)]
        widths = [usable * 0.62, usable * 0.18, usable * 0.20]
        for r in with_items:
            data = [[_p("Item", _head), _p("Units Sold", right_head), _p("Total Sales", right_head)]]
            for item in r["items"]:
                data.append([
                    _p(item["description"]),
                    _p(_units(item["quantity"]), right_cell),
                    _p(_money(item["total"]), right_cell),
                ])
            data.append([
                _p(f"{r['store_name']} total", _cell_bold),
                _p(_units(r["quantity"]), right_bold),
                _p(_money(r["total"]), right_bold),
            ])
            t = Table(data, colWidths=widths, repeatRows=1)
            t.setStyle(_table_style(True, len(data)))
            heading = Paragraph(escape(r["store_name"]), _h3)
            # Keep a store's heading with the start of its table; long tables
            # still split across pages with the header row repeated.
            if len(data) <= 25:
                story.append(KeepTogether([heading, t, Spacer(1, 6)]))
            else:
                story += [heading, t, Spacer(1, 6)]

    if without_items:
        story += [
            Spacer(1, 8),
            Paragraph(
                "<b>No matching sales this period:</b> " + escape(", ".join(without_items)),
                _meta,
            ),
        ]

    def _footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(doc.leftMargin, 0.35 * inch, "WOSV Dashboard – Category Sales")
        canvas.drawRightString(letter[0] - doc.rightMargin, 0.35 * inch, f"Page {_doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buffer.seek(0)
    return buffer


def pdf_filename(report):
    """e.g. category_sales_Rolling-Trays_2026-11-01_to_2026-11-09.pdf"""
    mode = report["search_mode"]
    term = {
        "Category": report.get("selected_category"),
        "Merged Category": report.get("selected_category"),
        "Keyword": report.get("keyword"),
        "UPC": report.get("upc"),
    }.get(mode) or mode
    slug = "".join(c if c.isalnum() else "-" for c in str(term)).strip("-")[:40] or mode
    return f"category_sales_{slug}_{report['start_date']}_to_{report['end_date']}.pdf"

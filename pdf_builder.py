"""
pdf_builder.py

Small helper for turning a title + column headers + rows into a
formatted PDF, similar in spirit to Lightspeed's own printed reports.
"""

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

styles = getSampleStyleSheet()

_header_style = ParagraphStyle(
    "TableHeader", parent=styles["Normal"], fontName="Helvetica-Bold",
    fontSize=8, leading=9.5, textColor=colors.white,
)
_cell_style = ParagraphStyle(
    "TableCell", parent=styles["Normal"], fontName="Helvetica",
    fontSize=8, leading=9.5,
)


def build_pdf_report(
    filepath: str,
    title: str,
    subtitle: str,
    headers: list[str],
    rows: list[list[str]],
    totals_row: list[str] | None = None,
    col_widths: list[float] | None = None,
):
    """
    filepath: output .pdf path
    title: report name, e.g. "Sales Tax"
    subtitle: e.g. "WOSV Oak Lawn -- 2026-08-01 to 2026-08-31"
    headers: column header strings
    rows: list of row value lists (already formatted as strings)
    totals_row: optional final summary row, bolded
    col_widths: optional explicit column widths in inches; auto-spread if omitted
    """
    doc = SimpleDocTemplate(
        filepath,
        pagesize=letter,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
    )

    story = [
        Paragraph(title, styles["Title"]),
        Paragraph(subtitle, styles["Normal"]),
        Spacer(1, 14),
    ]

    # Wrap every cell in a Paragraph so long text wraps within its column
    # instead of overflowing into the next one.
    header_cells = [Paragraph(str(h), _header_style) for h in headers]
    body_rows = [[Paragraph(str(v), _cell_style) for v in row] for row in rows]
    table_data = [header_cells] + body_rows
    if totals_row:
        totals_style = ParagraphStyle("TotalsCell", parent=_cell_style, fontName="Helvetica-Bold")
        table_data.append([Paragraph(str(v), totals_style) for v in totals_row])

    if col_widths is None:
        usable_width = 7.5 * inch
        col_widths = [usable_width / len(headers)] * len(headers)
    else:
        col_widths = [w * inch for w in col_widths]

    table = Table(table_data, colWidths=col_widths, repeatRows=1)

    style_commands = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f3542")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f2f6")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#ced6e0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if totals_row:
        last_row = len(table_data) - 1
        style_commands += [
            ("FONTNAME", (0, last_row), (-1, last_row), "Helvetica-Bold"),
            ("BACKGROUND", (0, last_row), (-1, last_row), colors.HexColor("#dfe4ea")),
        ]

    table.setStyle(TableStyle(style_commands))
    story.append(table)

    doc.build(story)

"""
Builds the monthly sales PDF report for the Pace Calculator page.

Separate from pace_calculator_page.py so the PDF layout can be tweaked
without touching the page logic itself.
"""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def build_monthly_pdf(store_rows, north_total, south_total, month, year):
    """
    store_rows: list of (store_name, total) tuples, already sorted however
    you want them to appear.
    north_total / south_total: floats for the summary at the bottom.
    month: 1-12
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=54, bottomMargin=54, leftMargin=54, rightMargin=54,
    )
    styles = getSampleStyleSheet()
    story = []

    month_name = MONTH_NAMES[month - 1]
    story.append(Paragraph(f"Monthly Sales Report - {month_name} {year}", styles["Title"]))
    story.append(Spacer(1, 16))

    table_data = [["Store", "Monthly Total"]]
    for store_name, total in store_rows:
        table_data.append([store_name, f"${total:,.2f}"])

    table = Table(table_data, colWidths=[300, 150])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f2f6")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)
    story.append(Spacer(1, 24))

    summary_data = [
        ["North Stores Total", f"${north_total:,.2f}"],
        ["South Stores Total", f"${south_total:,.2f}"],
    ]
    summary_table = Table(summary_data, colWidths=[300, 150])
    summary_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEABOVE", (0, 0), (-1, 0), 1, colors.HexColor("#333333")),
    ]))
    story.append(summary_table)

    doc.build(story)
    buffer.seek(0)
    return buffer

"""
Builds the "ready to pay" PDF: one section per store, listing each invoice
number and amount, with a store subtotal.
"""
import io
from datetime import date

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet


def build_ready_to_pay_pdf(ready_by_store):
    """
    ready_by_store: dict of {store_sheet_name: [row_with_matched_po, ...]}
    Returns BytesIO of the PDF.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph("Touch Tell - Ready to Pay", styles["Title"]))
    elements.append(Paragraph(date.today().strftime("%B %d, %Y"), styles["Normal"]))
    elements.append(Spacer(1, 0.25 * inch))

    grand_total = 0.0

    for store_name in sorted(ready_by_store.keys()):
        rows = ready_by_store[store_name]
        if not rows:
            continue

        elements.append(Paragraph(store_name, styles["Heading2"]))

        table_data = [["Invoice #", "Amount"]]
        store_total = 0.0
        for row in rows:
            amount = row["amount"]
            store_total += amount
            table_data.append([str(row["invoice_number"]), f"${amount:,.2f}"])
        table_data.append(["Store Total", f"${store_total:,.2f}"])
        grand_total += store_total

        table = Table(table_data, colWidths=[3 * inch, 2 * inch])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
            ("GRID", (0, 0), (-1, -2), 0.5, colors.HexColor("#cccccc")),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ]))
        elements.append(table)
        elements.append(Spacer(1, 0.3 * inch))

    elements.append(Spacer(1, 0.2 * inch))
    elements.append(Paragraph(f"Grand Total: ${grand_total:,.2f}", styles["Heading2"]))

    doc.build(elements)
    buffer.seek(0)
    return buffer

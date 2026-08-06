"""
Matches unresolved Touch Tell invoice sheet rows against Lightspeed
purchase orders and sorts each into: ready to pay, or an exception with a
plain-English reason.
"""
from lightspeed_po import READY_STAGES

AMOUNT_TOLERANCE = 0.01  # cents-level rounding slack


def match_store_invoices(sheet_rows, purchase_orders):
    """
    sheet_rows: rows from sheets_client.read_invoice_rows() for ONE store,
        already filtered to rows with status is None (unresolved).
    purchase_orders: rows from lightspeed_po.fetch_purchase_orders_for_vendor()
        for that same store.

    Returns (ready, exceptions):
      ready: list of sheet_row dicts with a "matched_po" key added.
      exceptions: list of (sheet_row, reason_string) tuples.
    """
    po_by_reference = {}
    for po in purchase_orders:
        po_by_reference.setdefault(po["reference_number"], []).append(po)

    ready = []
    exceptions = []

    for row in sheet_rows:
        invoice_number = str(row["invoice_number"]).strip()
        matches = po_by_reference.get(invoice_number, [])

        if not matches:
            exceptions.append((row, "No matching PO found in Lightspeed for this invoice/reference number."))
            continue

        if len(matches) > 1:
            exceptions.append((row, f"{len(matches)} purchase orders share this reference number - needs manual review."))
            continue

        po = matches[0]

        if po["stage"] == "unknown":
            exceptions.append((row, f"PO stage '{po['raw_status']}' isn't recognized - needs the status mapping checked."))
            continue

        if po["stage"] not in READY_STAGES:
            exceptions.append((row, f"PO is still in '{po['stage'].replace('_', ' ').title()}' stage (needs to be Ordered or later)."))
            continue

        if row["amount"] is None:
            exceptions.append((row, "Sheet amount isn't a valid number."))
            continue

        if abs(row["amount"] - po["total"]) > AMOUNT_TOLERANCE:
            exceptions.append((
                row,
                f"Amount mismatch: sheet shows ${row['amount']:,.2f}, Lightspeed PO total is ${po['total']:,.2f}."
            ))
            continue

        row_with_match = dict(row)
        row_with_match["matched_po"] = po
        ready.append(row_with_match)

    return ready, exceptions

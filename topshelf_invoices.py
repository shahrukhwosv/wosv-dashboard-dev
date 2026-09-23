"""
Top Shelf Novelties daily invoice log: invoices from the Salesgent ERP,
with product cost, shipping cost (from ShipStation when the invoice didn't
charge shipping), profit and margin - saved to a Google Sheet tab so any
date or date range can be looked up later.

SHEET: a tab named "Top Shelf Invoices" inside the same spreadsheet as the
Pace Calculator's daily sales log (PACE_LOG_SHEET_ID). Created
automatically on first run. One row per invoice.

HOW EACH ROW IS CALCULATED:
  Amount          = invoice total
  Shipping        = 0 if the invoice charged the customer shipping,
                    otherwise the ShipStation label cost
  Product Sales   = Amount - Tax - Shipping Charged
  Product Cost    = sum of (cost price x quantity) over the invoice's items
  Profit          = Product Sales - Product Cost - Shipping
  Margin %        = Profit / Product Sales

REFRESH SCHEDULE (nightly_refresh.py):
  Each night re-pulls the last RESYNC_DAYS days, not just yesterday - a
  label printed the day after the invoice, or an invoice edited later,
  gets picked up on the following nights' runs. Days before that window
  are left alone once written. Any gap (e.g. the job was down) is filled
  from the last logged date forward, and on the very first run the log
  is backfilled from BACKFILL_START.
"""
import os
from datetime import date, datetime, timedelta

import shipstation_client
from sales_pace import store_local_today, _pace_log_sheet_id
from sheets_client import _load_credentials
from topshelf_erp import ERP_TIMEZONE, ErpClient

WORKSHEET_NAME = "Top Shelf Invoices"
RESYNC_DAYS = 3
# First run backfills from here. Default: first of the current month.
BACKFILL_START = os.getenv("TOPSHELF_BACKFILL_START")  # e.g. "2026-09-01"

HEADER = [
    "Date", "Invoice #", "Customer", "Company", "Amount", "Tax",
    "Shipping Charged", "Shipping", "Shipping Source", "Product Sales",
    "Product Cost", "Profit", "Margin %", "Status", "Synced At",
]

# Shipping Source values
SRC_CHARGED = "Charged on invoice"
SRC_SHIPSTATION = "ShipStation"
SRC_NOT_FOUND = "Not found in ShipStation"
SRC_NOT_CONFIGURED = "ShipStation not connected"
SRC_NO_SHIPMENT = "No shipment"


# ---------------------------------------------------------------------------
# Building rows
# ---------------------------------------------------------------------------

def _money(v):
    return round(float(v or 0), 2)


def build_invoice_row(erp, day, listed):
    """One sheet row (as a dict) for one invoice from the ERP's list."""
    invoice_id = listed["id"]
    header = erp.invoice_header(invoice_id)
    items = erp.invoice_line_items(invoice_id)

    amount = _money(header.get("totalAmount", listed.get("totalAmount")))
    tax = _money(header.get("taxAmount"))
    shipping_charged = _money(header.get("shippingAmount"))
    product_sales = _money(amount - tax - shipping_charged)
    product_cost = _money(sum((li.get("costPrice") or 0) * (li.get("quantity") or 0) for li in items))

    if shipping_charged > 0:
        shipping, source = 0.0, SRC_CHARGED
    else:
        ss_ids = [
            s.get("channelOrderId") for s in erp.invoice_shipments(invoice_id)
            if (s.get("channelName") or "").lower() == "ship-station" and s.get("channelOrderId")
        ]
        if not shipstation_client.is_configured():
            shipping, source = 0.0, SRC_NOT_CONFIGURED
        else:
            cost = None
            for ss_id in ss_ids or [None]:
                c = shipstation_client.label_cost(shipstation_order_id=ss_id, order_number=invoice_id)
                if c is not None:
                    cost = (cost or 0) + c
            if cost is None:
                shipping, source = 0.0, (SRC_NOT_FOUND if ss_ids else SRC_NO_SHIPMENT)
            else:
                shipping, source = _money(cost), SRC_SHIPSTATION

    profit = _money(product_sales - product_cost - shipping)
    margin = round(profit / product_sales * 100, 1) if product_sales > 0 else ""

    return {
        "Date": day.isoformat(),
        "Invoice #": invoice_id,
        "Customer": listed.get("dbaName") or listed.get("companyName") or listed.get("customerName") or "",
        "Company": listed.get("companyName") or "",
        "Amount": amount,
        "Tax": tax,
        "Shipping Charged": shipping_charged,
        "Shipping": shipping,
        "Shipping Source": source,
        "Product Sales": product_sales,
        "Product Cost": product_cost,
        "Profit": profit,
        "Margin %": margin,
        "Status": listed.get("status") or "",
        "Synced At": datetime.now(ERP_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
    }


def build_day(erp, day):
    """All invoice rows for one day, skipping cancelled/voided invoices."""
    rows = []
    for listed in erp.list_invoices(day):
        status = (listed.get("status") or "").lower()
        if "cancel" in status or "void" in status:
            continue
        rows.append(build_invoice_row(erp, day, listed))
    return sorted(rows, key=lambda r: int(r["Invoice #"]))


# ---------------------------------------------------------------------------
# Sheet I/O
# ---------------------------------------------------------------------------

def _worksheet():
    import gspread
    client = gspread.authorize(_load_credentials())
    sh = client.open_by_key(_pace_log_sheet_id())
    try:
        ws = sh.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=len(HEADER))
    if not ws.row_values(1):
        ws.update(values=[HEADER], range_name="A1")
    return ws


def read_log():
    """The whole log as a pandas DataFrame (numeric columns as floats,
    Date as datetime.date)."""
    import pandas as pd
    values = _worksheet().get_all_values()
    if len(values) < 2:
        return pd.DataFrame(columns=HEADER)
    df = pd.DataFrame(values[1:], columns=values[0])
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    df["Invoice #"] = pd.to_numeric(df["Invoice #"], errors="coerce")
    for col in ["Amount", "Tax", "Shipping Charged", "Shipping", "Product Sales", "Product Cost", "Profit", "Margin %"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["Date"])


def write_days(rows_by_day):
    """Replaces every row for the given days with the new rows (so a
    re-pulled day never ends up duplicated). Writes the new full table
    first and only then trims leftover rows, so a failure midway never
    leaves the sheet emptier than before."""
    ws = _worksheet()
    values = ws.get_all_values()
    existing = values[1:] if len(values) > 1 else []
    days = {d.isoformat() for d in rows_by_day}

    kept = [r for r in existing if r and r[0] not in days]
    new = [[row[h] for h in HEADER] for rows in rows_by_day.values() for row in rows]
    table = sorted(kept + new, key=lambda r: (r[0], int(r[1]) if str(r[1]).isdigit() else 0))

    ws.update(values=[HEADER] + table, range_name="A1", value_input_option="RAW")  # RAW keeps dates as plain "YYYY-MM-DD" text so re-pulled days always match
    old_len, new_len = len(values), len(table) + 1
    if old_len > new_len:
        ws.batch_clear([f"A{new_len + 1}:{chr(ord('A') + len(HEADER) - 1)}{old_len}"])


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _default_backfill_start(today):
    if BACKFILL_START:
        return date.fromisoformat(BACKFILL_START)
    return today.replace(day=1)


def sync_days(days, progress=print):
    """Pulls the given days from the ERP (+ShipStation) and writes them."""
    erp = ErpClient()
    rows_by_day = {}
    for day in days:
        rows_by_day[day] = build_day(erp, day)
        progress(f"[top shelf] {day.isoformat()}: {len(rows_by_day[day])} invoice(s)")
    if rows_by_day:
        write_days(rows_by_day)
    return sum(len(r) for r in rows_by_day.values())


def nightly_sync(progress=print):
    """What nightly_refresh.py runs: fill any gap since the last logged
    day, and re-pull the last RESYNC_DAYS days."""
    today = store_local_today()
    yesterday = today - timedelta(days=1)

    log = read_log()
    if log.empty:
        start = _default_backfill_start(today)
    else:
        start = min(log["Date"].max() + timedelta(days=1), yesterday - timedelta(days=RESYNC_DAYS - 1))

    days = [start + timedelta(days=i) for i in range((yesterday - start).days + 1)]
    progress(f"[top shelf] Syncing {len(days)} day(s): {start.isoformat()} to {yesterday.isoformat()}")
    count = sync_days(days, progress)
    progress(f"[top shelf] Done - {count} invoice row(s) written.")
    return count

"""
Etsy order profit log: every Etsy order with Etsy's fees, the ShipStation
label cost and the Top Shelf ERP product cost subtracted - saved to a
Google Sheet tab so any date or date range can be looked up later. Same
pattern as topshelf_invoices.py.

SHEET: tabs inside the pace log spreadsheet (PACE_LOG_SHEET_ID), created
automatically on first run:
  "Etsy Orders"      one row per order
  "Etsy Other Fees"  fees not tied to one order (new listing fees,
                     listing renewals, subscriptions, ...) - one row per
                     ledger entry, so the page can show a true net for a
                     period

HOW EACH ORDER ROW IS CALCULATED:
  Total          = what the buyer paid (Etsy "grandtotal": items +
                   shipping charged + sales tax - discounts)
  Tax            = sales tax/VAT. Etsy collects and pays this to the
                   state itself, so it's never the shop's money
  Refunds        = anything refunded to the buyer
  Revenue        = Total - Tax - Refunds
  Etsy Fees      = every fee Etsy charged for the order (transaction fee
                   on items and on shipping, payment processing fee,
                   offsite ads fee, the $0.20 auto-renew when an item
                   sells, ...), minus any fees Etsy credited back on a
                   refund. Pulled from the shop's payment-account ledger
                   and the order's payment record.
  Shipping       = label cost: ShipStation (matched on the Etsy order
                   number) and/or a label bought on Etsy (from the
                   ledger, minus any refund for a voided label)
  Product Cost   = for each item: Top Shelf ERP cost price x quantity,
                   matched by the Etsy SKU = ERP UPC
  Profit         = Revenue - Etsy Fees - Shipping - Product Cost
  Margin %       = Profit / Revenue

REFRESH SCHEDULE (nightly_refresh.py):
  Etsy posts some fees days after the sale (e.g. the shipping transaction
  fee when the order ships) and labels are often printed the next day, so
  each night re-pulls the last RESYNC_DAYS days. Gaps are filled from the
  last logged day, and the first run backfills from BACKFILL_START.
"""
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import shipstation_client
from etsy_client import EtsyClient, money
from sales_pace import store_local_today, _pace_log_sheet_id
from sheets_client import _load_credentials
from topshelf_erp import ERP_TIMEZONE, ErpClient

WORKSHEET_NAME = "Etsy Orders"
OTHER_FEES_WORKSHEET = "Etsy Other Fees"
RESYNC_DAYS = 7
BACKFILL_START = os.getenv("ETSY_BACKFILL_START")  # e.g. "2026-09-01"; default: first of this month
TZ = ERP_TIMEZONE  # America/Chicago

HEADER = [
    "Date", "Order #", "Buyer", "Items", "Total", "Tax", "Refunds", "Revenue",
    "Etsy Fees", "Fee Detail", "Shipping", "Shipping Source", "Product Cost",
    "Cost Status", "Profit", "Margin %", "Status", "Synced At",
]
OTHER_FEES_HEADER = ["Date", "Entry ID", "Type", "Description", "Amount", "Synced At"]

# Shipping Source values
SRC_SHIPSTATION = "ShipStation"
SRC_ETSY_LABEL = "Etsy label"
SRC_BOTH = "ShipStation + Etsy label"
SRC_NOT_FOUND = "No label found"
SRC_NOT_CONFIGURED = "ShipStation not connected"

COST_OK = "OK"

# Ledger entry types that are NOT fees: the sale itself, payouts, sales
# tax passing through, and buyer refunds (already subtracted via the
# order's refunds). Compared lowercase.
NOT_FEE_TYPES = {"payment", "sale", "deposit", "disburse", "disburse2", "sales_tax", "vat", "refund"}


def _money(v):
    return round(float(v or 0), 2)


def _is_fee(entry):
    t = (entry.get("ledger_type") or "").lower()
    return t not in NOT_FEE_TYPES and not t.startswith("disburse") and "tax" not in t


def _is_label(entry):
    """Shipping labels bought on Etsy (and refunds of voided ones). These
    are shipping cost, not an Etsy fee."""
    return "shipping_label" in (entry.get("ledger_type") or "").lower()


def _entry_time(entry):
    ts = entry.get("created_timestamp") or entry.get("create_date") or 0
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _local_date(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(TZ).date()


# ---------------------------------------------------------------------------
# Fees: match ledger entries to orders
# ---------------------------------------------------------------------------

def assign_fees(receipts, entries):
    """Splits fee ledger entries into {receipt_id: [entries]} and a list
    of entries not tied to any of these orders. An entry belongs to an
    order when its reference_id is the order's receipt id or one of its
    line items' transaction ids; listing-level entries (like the auto-renew
    when an item sells) go to the nearest-in-time order for that listing.

    Etsy shipping labels are matched by, in order: the order's shipment id,
    the order number appearing in the entry's description, or - failing
    both - the order Etsy marked shipped closest in time (within 6 hours)
    that doesn't already have a label."""
    by_id, by_listing, shipped = {}, defaultdict(list), []
    for r in receipts:
        rid = str(r["receipt_id"])
        by_id[rid] = rid
        for sh in r.get("shipments") or []:
            if sh.get("receipt_shipping_id"):
                by_id[str(sh["receipt_shipping_id"])] = rid
            ts = sh.get("shipment_notification_timestamp") or sh.get("created_timestamp")
            if ts:
                shipped.append((int(ts), rid))
        for t in r.get("transactions") or []:
            by_id[str(t.get("transaction_id"))] = rid
            by_listing[str(t.get("listing_id"))].append((int(r.get("create_timestamp") or r.get("created_timestamp") or 0), rid))

    per_order, unmatched, pending_labels = defaultdict(list), [], []
    for e in entries:
        if not _is_fee(e):
            continue
        ref = str(e.get("reference_id") or "")
        rid = by_id.get(ref)
        if rid is None and _is_label(e):
            for num in re.findall(r"\d{8,}", e.get("description") or ""):
                if num in by_id:
                    rid = by_id[num]
                    break
            if rid is None:
                pending_labels.append(e)
                continue
        if rid is None and ref in by_listing:
            when = _entry_time(e).timestamp()
            ts, candidate = min(by_listing[ref], key=lambda c: abs(c[0] - when))
            if abs(ts - when) <= 2 * 86400:
                rid = candidate
        if rid is None:
            unmatched.append(e)
        else:
            per_order[rid].append(e)

    # Labels that couldn't be tied to an order by id: nearest shipment.
    for e in sorted(pending_labels, key=lambda x: _entry_time(x)):
        when = _entry_time(e).timestamp()
        labelled = {rid for rid, es in per_order.items() if any(_is_label(x) and (x.get("amount") or 0) < 0 for x in es)}
        options = [(abs(ts - when), rid) for ts, rid in shipped if rid not in labelled and abs(ts - when) <= 6 * 3600]
        if (e.get("amount") or 0) < 0 and options:
            per_order[min(options)[1]].append(e)
        else:
            unmatched.append(e)
    return per_order, unmatched


def _fee_summary(entries, payments):
    """Total fee for an order (positive number) and a short breakdown."""
    parts = defaultdict(float)
    for e in entries:
        if _is_label(e):
            continue  # counted as shipping, see build_order_row
        parts[(e.get("ledger_type") or "fee").lower()] += -(e.get("amount") or 0) / 100.0
    # Payment processing fees live on the payment record. Only add them if
    # the ledger didn't already list them, so they're never counted twice.
    if not any("processing" in k for k in parts):
        for p in payments:
            fee = p.get("adjusted_fees") if p.get("adjusted_fees") is not None else p.get("amount_fees")
            parts["processing"] += money(fee)
    total = _money(sum(parts.values()))
    detail = ", ".join(f"{k} ${v:,.2f}" for k, v in sorted(parts.items()) if abs(v) >= 0.005)
    return total, detail


# ---------------------------------------------------------------------------
# Product cost: Etsy SKU = ERP UPC
# ---------------------------------------------------------------------------

class CostLookup:
    def __init__(self, erp):
        self.erp = erp
        self._cache = {}

    def unit_cost(self, upc):
        """(cost or None, problem or None). Several ERP products can share
        one UPC (color variants) - the highest cost is used so profit is
        never overstated."""
        upc = str(upc or "").strip()
        if not upc:
            return None, "no SKU on Etsy listing"
        if upc not in self._cache:
            matches = self.erp.products_by_upc(upc)
            costs = [float(p.get("costPrice") or 0) for p in matches if p.get("costPrice")]
            if not matches:
                self._cache[upc] = (None, f"UPC {upc} not in ERP")
            elif not costs:
                self._cache[upc] = (None, f"UPC {upc} has no cost in ERP")
            else:
                self._cache[upc] = (max(costs), None)
        return self._cache[upc]


# ---------------------------------------------------------------------------
# Building rows
# ---------------------------------------------------------------------------

def build_order_row(etsy, costs, receipt, fee_entries):
    rid = receipt["receipt_id"]
    total = money(receipt.get("grandtotal"))
    tax = _money(money(receipt.get("total_tax_cost")) + money(receipt.get("total_vat_cost")))
    refunds = _money(sum(money(r.get("amount")) for r in receipt.get("refunds") or []))
    revenue = _money(total - tax - refunds)

    fees, fee_detail = _fee_summary(fee_entries, etsy.receipt_payments(rid))

    label_entries = [e for e in fee_entries if _is_label(e)]
    etsy_label = _money(-sum((e.get("amount") or 0) for e in label_entries) / 100.0) if label_entries else None
    ss_cost = shipstation_client.label_cost(order_number=rid) if shipstation_client.is_configured() else None

    if ss_cost is not None and etsy_label is not None:
        shipping, ship_src = _money(ss_cost + etsy_label), SRC_BOTH
    elif ss_cost is not None:
        shipping, ship_src = _money(ss_cost), SRC_SHIPSTATION
    elif etsy_label is not None:
        shipping, ship_src = etsy_label, SRC_ETSY_LABEL
    elif not shipstation_client.is_configured():
        shipping, ship_src = 0.0, SRC_NOT_CONFIGURED
    else:
        shipping, ship_src = 0.0, SRC_NOT_FOUND

    product_cost, problems, items = 0.0, [], []
    for t in receipt.get("transactions") or []:
        qty = int(t.get("quantity") or 0)
        sku = (t.get("sku") or "").strip()
        items.append(f"{qty}x {t.get('title') or ''}"[:150] + (f" [{sku}]" if sku else ""))
        unit, problem = costs.unit_cost(sku)
        if problem:
            problems.append(problem)
        else:
            product_cost += unit * qty
    product_cost = _money(product_cost)

    profit = _money(revenue - fees - shipping - product_cost)
    margin = round(profit / revenue * 100, 1) if revenue > 0 else ""

    return {
        "Date": _local_date(receipt.get("create_timestamp") or receipt.get("created_timestamp")).isoformat(),
        "Order #": rid,
        "Buyer": receipt.get("name") or "",
        "Items": " | ".join(items),
        "Total": total,
        "Tax": tax,
        "Refunds": refunds,
        "Revenue": revenue,
        "Etsy Fees": fees,
        "Fee Detail": fee_detail,
        "Shipping": shipping,
        "Shipping Source": ship_src,
        "Product Cost": product_cost,
        "Cost Status": COST_OK if not problems else "Missing: " + "; ".join(dict.fromkeys(problems)),
        "Profit": profit,
        "Margin %": margin,
        "Status": receipt.get("status") or "",
        "Synced At": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
    }


def _day_bounds_utc(first, last):
    start = datetime.combine(first, datetime.min.time(), tzinfo=TZ)
    end = datetime.combine(last, datetime.min.time(), tzinfo=TZ) + timedelta(days=1) - timedelta(seconds=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def build_days(days, progress=print):
    """Order rows and other-fee rows for the given days, grouped by day."""
    etsy, erp = EtsyClient(), ErpClient()
    costs = CostLookup(erp)
    start, end = _day_bounds_utc(min(days), max(days))
    wanted = {d.isoformat() for d in days}

    receipts = [
        r for r in etsy.receipts(start, end)
        if "cancel" not in (r.get("status") or "").lower() and r.get("is_paid", True)
    ]
    # Fees for these orders can post up to ~2 weeks later (shipping fee on
    # ship date, refund credits), so read the ledger past the last day.
    ledger_end = min(end + timedelta(days=14), datetime.now(timezone.utc))
    entries = etsy.ledger_entries(start - timedelta(days=1), ledger_end)
    per_order, unmatched = assign_fees(receipts, entries)
    progress(f"[etsy] {len(receipts)} order(s), {len(entries)} ledger entries")

    orders = {d: [] for d in days}
    for r in receipts:
        d = _local_date(r.get("create_timestamp") or r.get("created_timestamp"))
        if d.isoformat() not in wanted:
            continue
        orders[d].append(build_order_row(etsy, costs, r, per_order.get(str(r["receipt_id"]), [])))

    synced = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    other = {d: [] for d in days}
    for e in unmatched:
        d = _entry_time(e).astimezone(TZ).date()
        if d.isoformat() in wanted:
            other[d].append({
                "Date": d.isoformat(),
                "Entry ID": e.get("entry_id"),
                "Type": e.get("ledger_type") or "",
                "Description": e.get("description") or "",
                "Amount": _money(-(e.get("amount") or 0) / 100.0),
                "Synced At": synced,
            })
    for d in days:
        orders[d].sort(key=lambda r: int(r["Order #"]))
    return orders, other


# ---------------------------------------------------------------------------
# Sheet I/O
# ---------------------------------------------------------------------------

def _worksheet(name, header):
    import gspread
    client = gspread.authorize(_load_credentials())
    sh = client.open_by_key(_pace_log_sheet_id())
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(header))
    if not ws.row_values(1):
        ws.update(values=[header], range_name="A1")
    return ws


def _read(name, header, numeric):
    import pandas as pd
    values = _worksheet(name, header).get_all_values()
    if len(values) < 2:
        return pd.DataFrame(columns=header)
    df = pd.DataFrame(values[1:], columns=values[0])
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["Date"])


def read_log():
    return _read(WORKSHEET_NAME, HEADER, ["Order #", "Total", "Tax", "Refunds", "Revenue", "Etsy Fees",
                                          "Shipping", "Product Cost", "Profit", "Margin %"])


def read_other_fees():
    return _read(OTHER_FEES_WORKSHEET, OTHER_FEES_HEADER, ["Amount"])


def _write(name, header, rows_by_day):
    """Replaces every row for the given days (so a re-pulled day is never
    duplicated). Writes the full new table first, then trims leftovers."""
    ws = _worksheet(name, header)
    values = ws.get_all_values()
    existing = values[1:] if len(values) > 1 else []
    days = {d.isoformat() for d in rows_by_day}
    kept = [r for r in existing if r and r[0] not in days]
    new = [[str(row[h]) if h in ("Order #", "Entry ID") else row[h] for h in header]
           for rows in rows_by_day.values() for row in rows]
    table = sorted(kept + new, key=lambda r: (str(r[0]), str(r[1])))
    ws.update(values=[header] + table, range_name="A1", value_input_option="RAW")
    old_len, new_len = len(values), len(table) + 1
    if old_len > new_len:
        ws.batch_clear([f"A{new_len + 1}:{chr(ord('A') + len(header) - 1)}{old_len}"])


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_days(days, progress=print):
    days = sorted(days)
    orders, other = build_days(days, progress)
    _write(WORKSHEET_NAME, HEADER, orders)
    _write(OTHER_FEES_WORKSHEET, OTHER_FEES_HEADER, other)
    for d in days:
        progress(f"[etsy] {d.isoformat()}: {len(orders[d])} order(s), {len(other[d])} other fee(s)")
    return sum(len(r) for r in orders.values())


def nightly_sync(progress=print):
    """What nightly_refresh.py runs: fill any gap since the last logged
    day and re-pull the last RESYNC_DAYS days."""
    import etsy_client
    if not etsy_client.is_configured():
        progress("[etsy] Skipped - ETSY_API_KEY / ETSY_SHARED_SECRET / ETSY_REDIRECT_URI not set.")
        return 0

    today = store_local_today()
    yesterday = today - timedelta(days=1)
    log = read_log()
    if log.empty:
        start = date.fromisoformat(BACKFILL_START) if BACKFILL_START else today.replace(day=1)
    else:
        start = min(log["Date"].max() + timedelta(days=1), yesterday - timedelta(days=RESYNC_DAYS - 1))
    days = [start + timedelta(days=i) for i in range((yesterday - start).days + 1)]
    if not days:
        return 0
    progress(f"[etsy] Syncing {len(days)} day(s): {days[0].isoformat()} to {days[-1].isoformat()}")
    count = sync_days(days, progress)
    progress(f"[etsy] Done - {count} order row(s) written.")
    return count

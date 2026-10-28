"""
WOSV Lost Sales: WOSV-brand items that were on a Top Shelf sales order but
didn't ship on it (the ERP's Report > Lost Sales), and whether each one has
been made up since.

Why this exists: when a lost item is sent later, it goes out on a NEW sales
order / invoice, so the ERP's Lost Sales report never clears the original
line. This module works out which ones have actually been fulfilled since.

HOW A LOST LINE COUNTS AS FULFILLED (automatic, nothing to tick off):
  A lost line = (original sales order, product, customer, lost qty, date).
  It's fulfilled by any invoice that is
    - to the SAME customer (store),
    - for the SAME product,
    - dated AT OR AFTER the original sales order, and
    - NOT created from the original sales order itself (so the original
      order's own partial invoice never counts).
  Invoice quantity is applied to that customer's lost lines for the product
  oldest-first, so one later invoice of 3 can clear three older lost lines,
  and a smaller invoice leaves a line partly fulfilled with the rest still
  owed. A store's normal reorder of the item also counts - they got it.

Data (all from the ERP via topshelf_erp.ErpClient, read-only):
  - lost lines: lost_sales_by_product(), filtered to the WOSV brand
  - later invoices: product_sales_history() for each product involved
  - which sales order an invoice came from: invoice_sales_order_id(), only
    looked up for invoices that could match (same customer, late enough)

Brand: WOSV is brand id 9 in the ERP (WOSV_BRAND_ID env var to override).
Items only show up here once their Brand is set to WOSV on the product.
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd

import topshelf_erp
from sales_pace import store_local_today

WOSV_BRAND_ID = int(os.getenv("WOSV_BRAND_ID", "9"))
TZ = topshelf_erp.ERP_TIMEZONE


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_ts(value):
    """ERP timestamps come as "2026-10-05T16:28:48.000+00:00" (lost sales
    report) or "2026-10-06 19:59:41" (sales history) - both UTC. Returns an
    aware UTC datetime, or None."""
    if not value:
        return None
    s = str(value).strip().replace("T", " ")
    s = s.split(".")[0].split("+")[0].rstrip("Z")
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Matching (pure - no ERP calls, so it can be tested on saved data)
# ---------------------------------------------------------------------------

def lost_lines_from_report(lost_products):
    """Flattens the Lost Sales by Product report into one dict per lost
    sales-order line."""
    lines = []
    for p in lost_products:
        for o in p.get("lossSalesBySalesRepWithOrderDtoDtoList") or []:
            lost_qty = _num(o.get("lostQuantity"))
            if lost_qty <= 0:
                continue
            lines.append({
                "product_id": p.get("productId"),
                "product": p.get("productName") or "",
                "sku": p.get("sku") or "",
                "upc": p.get("upc") or "",
                "in_stock": p.get("availableQuantity"),
                "sales_order_id": o.get("salesOrderId"),
                "so_time": _parse_ts(o.get("insertedTimestamp")),
                "so_status": o.get("status") or "",
                "customer_id": o.get("customerId"),
                "customer": o.get("customerName") or "",
                "store": o.get("company") or o.get("customerName") or "",
                "rep": o.get("salesRepresentativeName") or "",
                "ordered": _num(o.get("orderQuantity")),
                "sold": _num(o.get("soldQuantity")),
                "lost_qty": lost_qty,
                "unit_cost": _num(o.get("costPrice")),
                "unit_price": _num(o.get("retailAfterDiscount") or o.get("sellingPrice")),
            })
    return lines


def match_fulfillments(lost_lines, history_by_product, invoice_sales_order):
    """Fills in fulfilled_qty / remaining / fulfilled_by on each lost line.

    history_by_product: {product_id: [invoice line dicts from
        product_sales_history()]}
    invoice_sales_order: function(invoice_id) -> the sales order id that
        invoice came from (or None). Only called for invoices that could
        actually match, to keep ERP calls down.
    """
    by_key = defaultdict(list)
    for line in lost_lines:
        line["fulfilled_qty"] = 0.0
        line["fulfilled_by"] = []  # [(invoice_id, invoice_time, qty_applied)]
        by_key[(line["product_id"], line["customer_id"])].append(line)

    for (product_id, customer_id), lines in by_key.items():
        lines.sort(key=lambda l: (l["so_time"] or datetime.min.replace(tzinfo=timezone.utc), l["sales_order_id"] or 0))
        earliest = lines[0]["so_time"]
        lost_so_ids = {l["sales_order_id"] for l in lines}

        invoices = []
        for inv in history_by_product.get(product_id) or []:
            if inv.get("customerId") != customer_id:
                continue
            if (inv.get("orderType") or "Order") != "Order":
                continue
            qty = _num(inv.get("totalQuantity")) - _num(inv.get("returnQuantity"))
            inv_time = _parse_ts(inv.get("insertedTimestamp"))
            if qty <= 0 or inv_time is None or (earliest and inv_time < earliest):
                continue
            invoices.append({"id": inv.get("orderId"), "time": inv_time, "qty": qty})
        invoices.sort(key=lambda i: (i["time"], i["id"] or 0))

        for inv in invoices:
            source_so = invoice_sales_order(inv["id"])
            left = inv["qty"]
            for line in lines:
                if left <= 0:
                    break
                remaining = line["lost_qty"] - line["fulfilled_qty"]
                if remaining <= 0:
                    continue
                if line["so_time"] and inv["time"] < line["so_time"]:
                    continue
                # The original order's own invoice is the sale that DID
                # happen, not a make-up shipment.
                if source_so is not None and source_so == line["sales_order_id"]:
                    continue
                applied = min(left, remaining)
                line["fulfilled_qty"] += applied
                line["fulfilled_by"].append((inv["id"], inv["time"], applied))
                left -= applied

    for line in lost_lines:
        line["remaining"] = max(line["lost_qty"] - line["fulfilled_qty"], 0.0)
    return lost_lines


# ---------------------------------------------------------------------------
# Pulling from the ERP
# ---------------------------------------------------------------------------

def year_start_utc(today=None):
    today = today or store_local_today()
    return datetime(today.year, 1, 1, tzinfo=TZ).astimezone(timezone.utc)


def fetch_lines(client=None, today=None, progress=None):
    """Pulls this year's WOSV lost lines and matches them. Returns the
    list of line dicts (see lost_lines_from_report + match_fulfillments)."""
    client = client or topshelf_erp.ErpClient()
    today = today or store_local_today()
    start = year_start_utc(today)
    end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc) - timedelta(seconds=1)

    report = client.lost_sales_by_product(start, end, brand_id=WOSV_BRAND_ID)
    lines = lost_lines_from_report(report)
    if not lines:
        return []

    history = {}
    product_ids = sorted({l["product_id"] for l in lines})
    for i, pid in enumerate(product_ids, 1):
        if progress:
            progress(f"Checking later invoices ({i}/{len(product_ids)})")
        history[pid] = client.product_sales_history(pid, start, end)

    cache = {}

    def invoice_so(invoice_id):
        if invoice_id not in cache:
            cache[invoice_id] = client.invoice_sales_order_id(invoice_id)
        return cache[invoice_id]

    return match_fulfillments(lines, history, invoice_so)


# ---------------------------------------------------------------------------
# Tables for the page
# ---------------------------------------------------------------------------

def _local_date(dt):
    return dt.astimezone(TZ).date() if dt else None


def to_tables(lines, today=None):
    """(open_df, fulfilled_df). Open = still owed (incl. partly fulfilled);
    fulfilled = fully made up."""
    today = today or store_local_today()
    open_rows, done_rows = [], []
    for l in lines:
        so_date = _local_date(l["so_time"])
        base = {
            "Order Date": so_date,
            "Sales Order #": l["sales_order_id"],
            "Store": l["store"],
            "Customer": l["customer"],
            "Product": l["product"],
            "SKU": l["sku"],
            "Lost Qty": l["lost_qty"],
        }
        invoices = ", ".join(f"#{inv_id}" for inv_id, _, _ in l["fulfilled_by"])
        if l["remaining"] > 0:
            open_rows.append({
                **base,
                "Still Owed": l["remaining"],
                "Days Waiting": (today - so_date).days if so_date else None,
                "In Stock Now": l["in_stock"],
                "Owed Value": round(l["remaining"] * l["unit_price"], 2),
                "Order Status": l["so_status"],
                "Partly Sent On": invoices,
                "Rep": l["rep"],
            })
        else:
            last_time = max(t for _, t, _ in l["fulfilled_by"])
            done_date = _local_date(last_time)
            done_rows.append({
                **base,
                "Fulfilled On": done_date,
                "Invoice #": invoices,
                "Days To Fulfill": (done_date - so_date).days if so_date and done_date else None,
                "Value": round(l["lost_qty"] * l["unit_price"], 2),
            })

    open_df = pd.DataFrame(open_rows)
    if not open_df.empty:
        open_df = open_df.sort_values(["Order Date", "Sales Order #", "Product"]).reset_index(drop=True)
    done_df = pd.DataFrame(done_rows)
    if not done_df.empty:
        done_df = done_df.sort_values(["Fulfilled On", "Order Date"], ascending=[False, True]).reset_index(drop=True)
    return open_df, done_df

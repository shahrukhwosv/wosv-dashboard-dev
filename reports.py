"""
reports.py

Builds the six monthly reports from Lightspeed data. Each function
returns (headers, rows, totals_row) where rows/totals_row are already
formatted as strings, ready to hand to pdf_builder or csv.writer.

Ported from the standalone Lightspeed reports tool to run against this
app's existing multi-store lightspeed_client.py (config, store_key)
instead of its own separate single-account LightspeedClient class. All
shop_id/shop_name filtering from the original has been dropped: that tool
needed it because one Lightspeed account could contain multiple shops;
here, each store is already its own separate Lightspeed account (see
lightspeed_client.py's docstring), so every record this pulls already
belongs to the one store being reported on - there's nothing left to
filter by.

CONFIDENCE NOTES (read before relying on this in production) - carried
over unchanged from the original tool, since the underlying field
assumptions haven't been tested against your live data yet:

  - Inventory Valuation, Item Summary, Sales Tax, Payments, Purchase
    Orders are built from documented Item / Sale / SaleLine / SalePayment
    / Order endpoints and fields. The overall shape should be right, but
    a couple of derived numbers (discount handling in Item Summary /
    Sales Tax, specifically) are reconstructed from field names alone --
    not yet tested against a live account. Generate a report for a month
    you can already see and diff the totals against what you'd normally
    pull from Lightspeed directly; if something's off by a consistent
    amount, that usually points to a specific field meaning pre- vs.
    post-discount.

  - Adds/Payouts is the least certain of the six. Lightspeed doesn't
    publicly document a dedicated "adds and withdraws" endpoint the way
    it does Sale/Item/Order. This implementation reads it off
    RegisterCount / RegisterCountAmounts (the register close/count
    records), which is a reasonable guess but genuinely needs to be
    checked against your account before you trust it. If it doesn't
    line up, Lightspeed support or your account rep can confirm the
    right endpoint.
"""

from collections import defaultdict
from datetime import date, timedelta

import lightspeed_client as ls


def money(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def iso_range(start: date, end_inclusive: date) -> tuple[str, str]:
    """Lightspeed timestamps are ISO 8601. End is made exclusive (next day)."""
    start_iso = f"{start.isoformat()}T00:00:00+00:00"
    end_exclusive = end_inclusive + timedelta(days=1)
    end_iso = f"{end_exclusive.isoformat()}T00:00:00+00:00"
    return start_iso, end_iso


def _between(field, start_iso, end_iso):
    """Same "between" operator style already proven elsewhere in
    lightspeed_client.py (see fetch_sales' timeStamp filter), applied here
    to whichever date field each report needs."""
    return (field, f"><,{start_iso},{end_iso}")


# ----------------------------------------------------------------------
# Shared data pull: completed sales with line items and payments
# ----------------------------------------------------------------------

def fetch_completed_sales(config, store_key, start: date, end: date) -> list[dict]:
    start_iso, end_iso = iso_range(start, end)
    params = [
        ("limit", 100),
        ("completed", "true"),
        _between("completeTime", start_iso, end_iso),
        ("load_relations", '["SaleLines","SalePayments"]'),
    ]
    sales = ls.fetch_all(config, store_key, "Sale.json", params=params, record_key="Sale")

    # Normalize nested relations to always be lists, and drop
    # voided/archived sales the same way the rest of the app does.
    valid_sales = []
    for sale in sales:
        if str(sale.get("voided", "false")).lower() == "true":
            continue
        if str(sale.get("archived", "false")).lower() == "true":
            continue
        for key in ("SaleLines", "SalePayments"):
            rel = sale.get(key)
            if rel is None:
                sale[key] = []
            elif isinstance(rel, dict):
                inner = rel.get(key[:-1], rel)  # e.g. SaleLines -> "SaleLine"
                if isinstance(inner, dict):
                    sale[key] = [inner]
                elif isinstance(inner, list):
                    sale[key] = inner
                else:
                    sale[key] = []
        valid_sales.append(sale)
    return valid_sales


def all_sale_lines(sales: list[dict]) -> list[dict]:
    lines = []
    for sale in sales:
        for line in sale.get("SaleLines", []):
            line["_sale"] = sale
            lines.append(line)
    return lines


def all_sale_payments(sales: list[dict]) -> list[dict]:
    payments = []
    for sale in sales:
        for pmt in sale.get("SalePayments", []):
            pmt["_sale"] = sale
            payments.append(pmt)
    return payments


# ----------------------------------------------------------------------
# 1. Inventory Valuation  (-> CSV)
# ----------------------------------------------------------------------

def build_inventory_valuation(config, store_key, lookups: dict, store_name: str):
    items = ls.fetch_all(
        config, store_key, "Item.json",
        params=[("limit", 100), ("load_relations", '["ItemShops"]')],
        record_key="Item",
    )

    headers = [
        "Store", "System ID", "Custom SKU", "Description", "Category", "Brand",
        "Manufacturer SKU", "Vendor", "UPC", "EAN", "QOH", "Cost",
    ]
    rows = []
    for item in items:
        item_shops = item.get("ItemShops", {}).get("ItemShop", []) if isinstance(item.get("ItemShops"), dict) else item.get("ItemShops", [])
        if isinstance(item_shops, dict):
            item_shops = [item_shops]

        # One store = one Lightspeed account, so there's normally exactly
        # one ItemShop entry here (unlike the original tool, which had to
        # pick the right one out of several shops sharing an account).
        # Sum qoh in case more than one ever shows up, and use the first
        # entry's cost, falling back to the item-level average cost.
        qoh = sum(float(s.get("qoh") or 0) for s in item_shops)
        cost = float((item_shops[0].get("averageCost") if item_shops else None) or item.get("avgCost") or 0)

        rows.append([
            store_name,
            item.get("itemID", ""),
            item.get("customSku", ""),
            item.get("description", ""),
            lookups["category"].get(str(item.get("categoryID")), ""),
            lookups["manufacturer"].get(str(item.get("manufacturerID")), ""),
            item.get("manufacturerSku", ""),
            lookups["vendor"].get(str(item.get("defaultVendorID")), ""),
            item.get("upc", ""),
            item.get("ean", ""),
            f"{qoh:g}",
            f"{cost:.4f}",
        ])

    return headers, rows, None


# ----------------------------------------------------------------------
# 2. Item Summary  (-> CSV)
# ----------------------------------------------------------------------

def build_item_summary(config, store_key, lookups: dict, sales: list[dict]):
    items = ls.fetch_all(
        config, store_key, "Item.json",
        params=[("limit", 100), ("load_relations", '["ItemShops"]')],
        record_key="Item",
    )
    item_by_id = {str(i["itemID"]): i for i in items}

    agg = defaultdict(lambda: {"sold": 0.0, "subtotal": 0.0, "discounts": 0.0, "tax": 0.0, "cost": 0.0})

    for line in all_sale_lines(sales):
        item_id = str(line.get("itemID"))
        qty = float(line.get("unitQuantity") or 0)
        unit_price = float(line.get("normalUnitPrice") or line.get("unitPrice") or 0)
        discount_amt = float(line.get("discountAmount") or 0)
        avg_cost = float(line.get("avgCost") or 0)
        tax1 = float(line.get("calcTax1") or 0)
        tax2 = float(line.get("calcTax2") or 0)

        a = agg[item_id]
        a["sold"] += qty
        a["subtotal"] += unit_price * qty
        a["discounts"] += discount_amt
        a["tax"] += tax1 + tax2
        a["cost"] += avg_cost * qty

    headers = [
        "System ID", "UPC", "EAN", "Custom SKU", "Manufact. SKU", "Description",
        "Stock", "Sold", "Subtotal", "Discounts", "Subtotal w/ Discounts", "Total",
        "Cost", "Profit", "Margin",
    ]
    rows = []
    for item_id, a in agg.items():
        item = item_by_id.get(item_id, {})
        subtotal_w_disc = a["subtotal"] - a["discounts"]
        total = subtotal_w_disc + a["tax"]
        profit = subtotal_w_disc - a["cost"]
        margin = (profit / subtotal_w_disc * 100) if subtotal_w_disc else 0.0

        item_shops = item.get("ItemShops", {}).get("ItemShop", []) if isinstance(item.get("ItemShops"), dict) else item.get("ItemShops", []) if item else []
        if isinstance(item_shops, dict):
            item_shops = [item_shops]
        stock = sum(float(s.get("qoh") or 0) for s in item_shops) if item_shops else ""

        rows.append([
            item_id,
            item.get("upc", ""),
            item.get("ean", ""),
            item.get("customSku", ""),
            item.get("manufacturerSku", ""),
            item.get("description", f"(item {item_id})"),
            f"{stock:g}" if stock != "" else "",
            f"{a['sold']:g}",
            money(a["subtotal"]),
            money(a["discounts"]),
            money(subtotal_w_disc),
            money(total),
            money(a["cost"]),
            money(profit),
            f"{margin:.2f}%",
        ])

    return headers, rows, None


# ----------------------------------------------------------------------
# 3. Sales Tax / Tax Category Summary  (-> PDF)
# ----------------------------------------------------------------------

def build_sales_tax_summary(lookups: dict, sales: list[dict], store_name: str, start: date, end: date):
    agg = defaultdict(lambda: {"subtotal": 0.0, "discounts": 0.0, "taxed": 0.0, "not_taxed": 0.0, "tax": 0.0, "cost": 0.0})

    for line in all_sale_lines(sales):
        tax_cat_id = str(line.get("taxCategoryID") or "")
        qty = float(line.get("unitQuantity") or 0)
        unit_price = float(line.get("normalUnitPrice") or line.get("unitPrice") or 0)
        discount_amt = float(line.get("discountAmount") or 0)
        avg_cost = float(line.get("avgCost") or 0)
        tax1 = float(line.get("calcTax1") or 0)
        tax2 = float(line.get("calcTax2") or 0)
        taxable = bool(line.get("tax"))

        a = agg[tax_cat_id]
        subtotal = unit_price * qty
        subtotal_w_disc = subtotal - discount_amt
        a["subtotal"] += subtotal
        a["discounts"] += discount_amt
        a["cost"] += avg_cost * qty
        a["tax"] += tax1 + tax2
        if taxable:
            a["taxed"] += subtotal_w_disc
        else:
            a["not_taxed"] += subtotal_w_disc

    headers = ["Tax Class", "Subtotal", "Discounts", "Subtotal w/ Discounts", "Taxed", "Not Taxed", "Tax", "Cost", "Profit"]
    rows = []
    grand = defaultdict(float)
    for tax_cat_id, a in agg.items():
        name = lookups["tax_category"].get(tax_cat_id, "Item")
        subtotal_w_disc = a["subtotal"] - a["discounts"]
        profit = subtotal_w_disc - a["cost"]
        rows.append([
            name, money(a["subtotal"]), money(a["discounts"]), money(subtotal_w_disc),
            money(a["taxed"]), money(a["not_taxed"]), money(a["tax"]), money(a["cost"]), money(profit),
        ])
        grand["subtotal"] += a["subtotal"]
        grand["discounts"] += a["discounts"]
        grand["taxed"] += a["taxed"]
        grand["not_taxed"] += a["not_taxed"]
        grand["tax"] += a["tax"]
        grand["cost"] += a["cost"]

    grand_subtotal_w_disc = grand["subtotal"] - grand["discounts"]
    grand_profit = grand_subtotal_w_disc - grand["cost"]
    totals_row = [
        "TOTAL", money(grand["subtotal"]), money(grand["discounts"]), money(grand_subtotal_w_disc),
        money(grand["taxed"]), money(grand["not_taxed"]), money(grand["tax"]), money(grand["cost"]), money(grand_profit),
    ]

    return headers, rows, totals_row


# ----------------------------------------------------------------------
# 4. Payments / Z-Out Store Close  (-> PDF)
# ----------------------------------------------------------------------

def build_payments_report(lookups: dict, sales: list[dict]):
    headers = ["Sale ID", "Date", "Type", "Amount", "Refund"]
    rows = []
    totals_by_type = defaultdict(float)
    refunds_by_type = defaultdict(float)

    for pmt in all_sale_payments(sales):
        sale = pmt["_sale"]
        amount = float(pmt.get("amount") or 0)
        type_name = lookups["payment_type"].get(str(pmt.get("paymentTypeID")), "Unknown")
        is_refund = amount < 0 or str(sale.get("voided", "false")).lower() == "true"

        rows.append([
            sale.get("saleID", ""),
            (sale.get("completeTime") or "")[:16].replace("T", " "),
            type_name,
            money(amount),
            "Yes" if is_refund else "No",
        ])

        if is_refund:
            refunds_by_type[type_name] += amount
        else:
            totals_by_type[type_name] += amount

    # Totals row(s) appended after the detail rows, mirroring the layout
    # of Lightspeed's own Payments / Z-Out report.
    totals_row = ["", "", "NET TOTAL", money(sum(totals_by_type.values()) + sum(refunds_by_type.values())), ""]

    summary_lines = []
    for t, amt in totals_by_type.items():
        summary_lines.append(f"{t}: {money(amt)}")
    for t, amt in refunds_by_type.items():
        summary_lines.append(f"{t} refunds: {money(amt)}")

    return headers, rows, totals_row, summary_lines


# ----------------------------------------------------------------------
# 5. Purchase Orders / Receiving Voucher Detail  (-> PDF)
# ----------------------------------------------------------------------

def build_purchase_orders_report(config, store_key, lookups: dict, start: date, end: date):
    start_iso, end_iso = iso_range(start, end)
    params = [
        ("limit", 100),
        _between("receivedDate", start_iso, end_iso),
        ("load_relations", '["OrderLines"]'),
    ]
    orders = ls.fetch_all(config, store_key, "Order.json", params=params, record_key="Order")

    headers = ["ID", "Status", "Reference #", "Vendor", "Order Date", "Received", "# Ordered", "# Received", "Total"]
    rows = []
    grand_total = 0.0
    for o in orders:
        lines = o.get("OrderLines", {}).get("OrderLine", []) if isinstance(o.get("OrderLines"), dict) else o.get("OrderLines", [])
        if isinstance(lines, dict):
            lines = [lines]
        num_ordered = sum(float(l.get("quantity") or 0) for l in lines)
        num_received = sum(float(l.get("numReceived") or 0) for l in lines)
        total = sum(float(l.get("total") or 0) for l in lines)
        grand_total += total

        rows.append([
            o.get("orderID", ""),
            "Finished" if o.get("complete") in (True, "true") else "Open",
            o.get("refNum", ""),
            lookups["vendor"].get(str(o.get("vendorID")), ""),
            (o.get("orderedDate") or "")[:10],
            (o.get("receivedDate") or "")[:10],
            f"{num_ordered:g}",
            f"{num_received:g}",
            money(total),
        ])

    totals_row = ["", "", "", "", "", "", "", "TOTAL", money(grand_total)]
    return headers, rows, totals_row


# ----------------------------------------------------------------------
# 6. Adds / Payouts  (-> PDF)   ** LOW CONFIDENCE, see module docstring **
# ----------------------------------------------------------------------

def build_adds_payouts_report(config, store_key, lookups: dict, start: date, end: date):
    start_iso, end_iso = iso_range(start, end)
    params = [
        ("limit", 100),
        _between("openTime", start_iso, end_iso),
        ("load_relations", '["RegisterCountAmounts","RegisterCountAmounts.PaymentType"]'),
    ]
    register_counts = ls.fetch_all(config, store_key, "RegisterCount.json", params=params, record_key="RegisterCount")

    headers = ["Register Count ID", "Open Time", "Type", "Amount", "Employee", "Notes"]
    rows = []
    total = 0.0
    for rc in register_counts:
        amounts = rc.get("RegisterCountAmounts", {})
        entries = amounts.get("RegisterCountAmount", []) if isinstance(amounts, dict) else amounts
        if isinstance(entries, dict):
            entries = [entries]
        for e in entries:
            amt = float(e.get("amount") or 0)
            pt = e.get("PaymentType", {})
            type_name = pt.get("name", "") if isinstance(pt, dict) else lookups["payment_type"].get(str(e.get("paymentTypeID")), "")
            rows.append([
                rc.get("registerCountID", ""),
                (rc.get("openTime") or "")[:16].replace("T", " "),
                type_name,
                money(amt),
                lookups["employee"].get(str(rc.get("openEmployeeID")), ""),
                rc.get("notes", ""),
            ])
            total += amt

    totals_row = ["", "", "", money(total), "", ""]
    return headers, rows, totals_row

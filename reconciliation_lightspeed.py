"""
Extension to lightspeed_client.py for the reconciliation tool. Kept as a
separate file so it doesn't disturb the working commission-report code.

Pulls sales with their payment/tender info attached, so we can filter down
to CARD-paid sales only (ValorPay only has card charges - cash sales have
nothing to reconcile against).

IMPORTANT: the exact field names Lightspeed uses for payment type can vary.
Run `python inspect_payment_types.py store_1` FIRST and compare its output
against the field-detection logic in `_extract_tender_type()` below before
trusting the reconciliation results. If detection isn't working, that
function is the only place that needs adjusting.
"""

from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
from lightspeed_client import api_get, api_get_full_url

# Each store's actual local timezone - ValorPay's report times are local
# (e.g. "07/05/2026 22:56" with no offset), while Lightspeed's API returns
# UTC timestamps, so we need to know each store's real zone to convert
# correctly. Default is Central.
#
# NOTE: both Princeton and Greenville are REAL Central-time stores, but both
# show sale times exactly 1 hour too early unless labeled "America/New_York"
# here. This is a workaround for an unconfirmed bug (suspected: the
# deployed environment isn't applying Daylight Saving Time correctly) -
# adding the `tzdata` package did NOT fix it, so the root cause is still
# unknown. Until it's found, any store showing a consistent 1-hour-too-early
# mismatch should be added here as "America/New_York" (NOT because it's
# really Eastern - it isn't - purely because that label produces the
# correct numeric offset given the current bug).
DEFAULT_TIMEZONE = "America/Chicago"
STORE_TIMEZONE_OVERRIDES = {
    "store_1": "America/New_York",  # Princeton - workaround, see note above
    "store_6": "America/New_York",  # Greenville - workaround, see note above
}


def _get_store_timezone(store_key):
    tz_name = STORE_TIMEZONE_OVERRIDES.get(store_key, DEFAULT_TIMEZONE)
    return ZoneInfo(tz_name)


# ValorPay's daily batch doesn't cut off at local midnight for every store -
# some settle on a later cutoff (e.g. Princeton's batch closes around 1am),
# meaning a sale at 12:30am still belongs to the PREVIOUS business day in
# Valor's world, even though Lightspeed (and our date picker) would call it
# "today." Setting a store's boundary here shifts the pulled Lightspeed
# window to match, so both sides agree on where one business day ends and
# the next starts. Default is midnight (no shift, matches old behavior).
DEFAULT_DAY_BOUNDARY = dt_time(0, 0)
STORE_DAY_BOUNDARY_OVERRIDES = {
    "store_1": dt_time(1, 0),  # Princeton - Valor batch closes ~1am
    "store_6": dt_time(3, 0),  # Greenville - observed charges as late as 2:29am, set with a buffer; confirm exact cutoff if issues persist
}


def _get_store_day_boundary(store_key):
    return STORE_DAY_BOUNDARY_OVERRIDES.get(store_key, DEFAULT_DAY_BOUNDARY)


def _get_card_amount(sale):
    """
    Looks at a raw Sale record's SalePayments and sums up ONLY the portion
    paid by card - NOT the sale's overall total. This matters for sales
    split between cash and card (e.g. $21.64 total, $15.64 on card + $6.00
    cash) - Valor only ever sees the $15.64 card portion, so that's what we
    need to compare against, not the full sale amount.

    Returns:
        (card_amount, had_any_unrecognized_payment_type)
        card_amount is None if the sale has no payment info at all (fully
        unknown - excluded from matching to avoid false positives).
        card_amount is 0.0 if every payment was recognized and none were
        card (i.e. a pure cash sale).
    """
    payments = sale.get("SalePayments", {})
    if isinstance(payments, dict):
        payments = payments.get("SalePayment", [])
    if isinstance(payments, dict):
        payments = [payments]
    if not payments:
        return None, False

    card_total = 0.0
    recognized_any = False
    had_unrecognized = False

    for p in payments:
        text_blobs = []
        pt = p.get("PaymentType", {})
        if isinstance(pt, dict):
            text_blobs.append(str(pt.get("name", "")))
        text_blobs.append(str(p.get("name", "")))
        text_blobs.append(str(p.get("paymentType", "")))
        combined = " ".join(text_blobs).lower()

        amount = float(p.get("amount", 0) or 0)

        is_cash = "cash" in combined and "card" not in combined and "credit" not in combined
        is_card = "card" in combined or "credit" in combined or "debit" in combined

        if is_card:
            card_total += amount
            recognized_any = True
        elif is_cash:
            recognized_any = True
        else:
            had_unrecognized = True

    if not recognized_any and had_unrecognized:
        return None, True  # every payment on this sale was an unrecognized type
    return card_total, had_unrecognized


def _extract_local_timestamp(sale, store_timezone):
    """
    Tries a couple of likely field names for the sale's completion time, and
    converts from Lightspeed's UTC to the store's local time (stripping tz
    info afterward) so it's directly comparable to ValorPay's local
    timestamps.
    """
    for field in ("completeTime", "timeStamp", "createTime"):
        raw = sale.get(field)
        if raw:
            try:
                utc_dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if utc_dt.tzinfo is None:
                    utc_dt = utc_dt.replace(tzinfo=timezone.utc)
                local_dt = utc_dt.astimezone(store_timezone)
                return local_dt.replace(tzinfo=None)
            except (ValueError, AttributeError):
                continue
    return None


def fetch_card_sales(config, store_key, start_date, end_date, employees):
    """
    Returns (card_sales, cash_count, unknown_count).

    card_sales: list of dicts for CARD-paid sales only:
        {sale_id, employee_name, total, timestamp, tender_type}
    timestamp is in the store's LOCAL time, matching ValorPay's report.

    Voided/archived sales are excluded (mirrors fetch_sales in
    lightspeed_client.py) - a voided sale shouldn't be flagged as a missing
    charge since it was never a real completed sale.

    Sales where tender type couldn't be determined ('unknown') are EXCLUDED
    from the card list by default, to avoid false "missing charge" flags on
    sales we can't confidently classify. They're reported separately (as a
    count) so nothing is silently dropped without you knowing.
    """
    limit = 100
    max_pages = 50
    store_timezone = _get_store_timezone(store_key)
    day_boundary = _get_store_day_boundary(store_key)

    # Convert local store dates to UTC boundaries, same approach as
    # lightspeed_client.fetch_sales, so "July 5" means the store's actual
    # BUSINESS day (which may not start/end at exact midnight - see
    # STORE_DAY_BOUNDARY_OVERRIDES above), not a UTC calendar day.
    start_local = datetime.combine(start_date, day_boundary, tzinfo=store_timezone)
    end_local_exclusive = datetime.combine(
        end_date + timedelta(days=1), day_boundary, tzinfo=store_timezone
    )
    start_utc = start_local.astimezone(timezone.utc)
    end_utc_inclusive = end_local_exclusive.astimezone(timezone.utc) - timedelta(seconds=1)

    def _ts(value):
        return value.isoformat(timespec="seconds")

    params = [
        ("limit", limit),
        ("completed", "true"),
        ("timeStamp", f"><,{_ts(start_utc)},{_ts(end_utc_inclusive)}"),
        ("load_relations", '["SaleLines","SalePayments","SalePayments.PaymentType"]'),
    ]
    data = api_get(config, store_key, "Sale.json", params=params)

    all_raw_sales = []
    page = 1
    seen_urls = set()
    while True:
        raw = data.get("Sale", [])
        if isinstance(raw, dict):
            raw = [raw]
        # Match lightspeed_client.fetch_sales: drop voided/archived records
        valid = [
            s for s in raw
            if str(s.get("voided", "false")).lower() != "true"
            and str(s.get("archived", "false")).lower() != "true"
        ]
        all_raw_sales.extend(valid)

        next_url = (data.get("@attributes", {}) or {}).get("next")
        if not next_url or next_url in seen_urls or page >= max_pages:
            break
        seen_urls.add(next_url)
        page += 1
        data = api_get_full_url(config, store_key, next_url)

    card_sales = []
    cash_count = 0
    unknown_count = 0

    for sale in all_raw_sales:
        card_amount, had_unrecognized_payment = _get_card_amount(sale)

        if card_amount is None:
            unknown_count += 1
            continue
        if card_amount == 0:
            cash_count += 1  # fully cash, nothing to reconcile against Valor
            continue

        timestamp = _extract_local_timestamp(sale, store_timezone)
        if timestamp is None:
            unknown_count += 1
            continue

        employee_id = sale.get("employeeID")

        card_sales.append({
            "sale_id": sale.get("saleID"),
            "employee_name": employees.get(employee_id, f"Employee {employee_id}"),
            "total": round(card_amount, 2),  # card portion only - may be less than the sale's full total if split with cash
            "timestamp": timestamp,
            "tender_type": "card",
            "had_unrecognized_payment": had_unrecognized_payment,
        })

    return card_sales, cash_count, unknown_count

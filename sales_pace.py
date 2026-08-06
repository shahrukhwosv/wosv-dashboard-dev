"""
Daily sales pace tracking.

Maintains a running per-store, per-day sales log in a dedicated Google
Sheet, and computes month-to-date / year-to-date pace projections from it.

Sales totals here are tax-inclusive: they use the same `total` field
(falling back to `calcTotal`) used elsewhere in the app, which Lightspeed
returns already including sales tax, not just the subtotal.

SHEET LAYOUT (worksheet "Daily Sales Log", in its own spreadsheet):
    Date        | Store    | Total Sales
    2026-07-22  | store_1  | 4210.55
    2026-07-22  | store_2  | 3199.10
    ...

One row per store per calendar day. The log only ever grows forward -
update_daily_log() fetches just the days that are missing since the last
run, so a normal daily page load costs one day's worth of Lightspeed API
calls per store, not a full month/year re-pull.
"""
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from lightspeed_client import fetch_sales
from sheets_client import get_worksheet

PACE_LOG_WORKSHEET_NAME = "Daily Sales Log"
STORE_TIMEZONE = ZoneInfo("America/Chicago")


def store_local_today():
    """
    "Today" as a calendar date in the stores' own timezone (Central), not
    whatever timezone the server happens to run in. Railway's servers run in
    UTC, which is 5-6 hours ahead of Central - without this, for several
    hours every evening the server would think a new day had already
    started while it's still the prior business day in Texas, throwing off
    which day counts as "yesterday" and how many days have elapsed in the
    month/year.
    """
    return datetime.now(STORE_TIMEZONE).date()


def _pace_log_sheet_id():
    """
    The pace log lives in its own Google Sheet (separate from the Touch Tell
    sheet). Create that sheet once, share it with the service account's
    client_email (same one already used for Touch Tell - see
    sheets_client.py's docstring), and set its ID here.

    Locally: set PACE_LOG_SHEET_ID in your environment or .env file.
    On Railway: add PACE_LOG_SHEET_ID as an environment variable, same
    pattern as STORES_CONFIG_JSON / GOOGLE_SERVICE_ACCOUNT_JSON.
    (The sheet ID is the long string in the sheet's URL between /d/ and /edit.)
    """
    sheet_id = os.getenv("PACE_LOG_SHEET_ID")
    if not sheet_id:
        raise RuntimeError(
            "PACE_LOG_SHEET_ID is not set. Create a new Google Sheet for the "
            "daily sales log, share it with the service account's "
            "client_email, and set PACE_LOG_SHEET_ID to its spreadsheet ID."
        )
    return sheet_id


def _ensure_header(ws):
    values = ws.get_all_values()
    if not values:
        ws.append_row(["Date", "Store", "Total Sales"])


def read_daily_log():
    """Returns the full log as a DataFrame with columns: date, store, total.
    `date` is a datetime.date, `total` is a float."""
    sheet_id = _pace_log_sheet_id()
    ws = get_worksheet(sheet_id, PACE_LOG_WORKSHEET_NAME)
    _ensure_header(ws)
    values = ws.get_all_values()

    if len(values) < 2:
        return pd.DataFrame(columns=["date", "store", "total"])

    rows = values[1:]
    df = pd.DataFrame(rows, columns=["date", "store", "total"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["total"] = pd.to_numeric(df["total"], errors="coerce")
    df = df.dropna(subset=["date", "store", "total"])
    return df


def _sale_amount(sale):
    """Tax-inclusive total for one sale (matches normalize_sale()'s `total`
    in lightspeed_client.py - includes sales tax, not just the subtotal)."""
    return float(sale.get("total", sale.get("calcTotal", 0)) or 0)


def get_daily_total(config, store_key, for_date):
    """Sum of tax-inclusive sale totals for one store on one calendar day."""
    sales = fetch_sales(config, store_key, for_date, for_date)
    return sum(_sale_amount(s) for s in sales)


def update_daily_log(config, store_keys, through_date=None, backfill_start=None):
    """
    Fetches and appends any missing days for each store, from the day after
    its last logged date through `through_date` (defaults to yesterday -
    today is intentionally excluded since it's not finished yet).

    If a store has no rows yet, it backfills from `backfill_start` (defaults
    to Jan 1 of the current year). This first run per store can be slow -
    see backfill_pace_log.py to run it once, up front, outside the Streamlit
    request cycle rather than triggering it from the page's refresh button.

    Returns the number of (store, day) rows appended.
    """
    if through_date is None:
        through_date = store_local_today() - timedelta(days=1)
    if backfill_start is None:
        backfill_start = date(through_date.year, 1, 1)

    existing = read_daily_log()
    sheet_id = _pace_log_sheet_id()
    ws = get_worksheet(sheet_id, PACE_LOG_WORKSHEET_NAME)

    new_rows = []
    for store_key in store_keys:
        store_rows = existing[existing["store"] == store_key]
        if store_rows.empty:
            start = backfill_start
        else:
            start = store_rows["date"].max() + timedelta(days=1)

        total_days = (through_date - start).days + 1
        if total_days <= 0:
            print(f"[{store_key}] Already up to date through {through_date.isoformat()}.")
            continue

        print(f"[{store_key}] Backfilling {total_days} day(s): {start.isoformat()} through {through_date.isoformat()}")

        current = start
        day_num = 0
        while current <= through_date:
            day_num += 1
            total = get_daily_total(config, store_key, current)
            new_rows.append([current.isoformat(), store_key, total])
            print(f"[{store_key}] Day {day_num} of {total_days} ({current.isoformat()}): ${total:,.2f}")
            current += timedelta(days=1)

    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")

    return len(new_rows)


def compute_pace(df, store_key, today=None):
    """
    Given the full log DataFrame, returns pace figures for one store:
    most recent day's total, projected monthly total, projected annual
    total, and the actual date those figures are calculated through.

    Uses a flat run-rate: (sales so far this period / days elapsed so far)
    * total days in the period - "days elapsed so far" is based on the last
    date actually present in the log (capped at yesterday, since today
    isn't finished yet), NOT blindly assumed to be yesterday. If the log
    hasn't been fetched in a day or two, this keeps the rate accurate
    instead of quietly diluting it with days that have no data yet.
    """
    if today is None:
        today = store_local_today()

    store_df = df[df["store"] == store_key]
    latest_possible = today - timedelta(days=1)

    if store_df.empty:
        return {
            "as_of": None,
            "yesterday_total": 0.0,
            "projected_monthly": 0.0,
            "projected_annual": 0.0,
        }

    as_of = min(store_df["date"].max(), latest_possible)

    as_of_row = store_df[store_df["date"] == as_of]
    as_of_total = float(as_of_row["total"].sum()) if not as_of_row.empty else 0.0

    month_start = as_of.replace(day=1)
    mtd_df = store_df[(store_df["date"] >= month_start) & (store_df["date"] <= as_of)]
    mtd_total = float(mtd_df["total"].sum())
    days_elapsed_month = (as_of - month_start).days + 1

    if as_of.month == 12:
        next_month_start = date(as_of.year + 1, 1, 1)
    else:
        next_month_start = date(as_of.year, as_of.month + 1, 1)
    days_in_month = (next_month_start - month_start).days

    projected_monthly = (
        (mtd_total / days_elapsed_month) * days_in_month if days_elapsed_month > 0 else 0.0
    )

    year_start = date(as_of.year, 1, 1)
    ytd_df = store_df[(store_df["date"] >= year_start) & (store_df["date"] <= as_of)]
    ytd_total = float(ytd_df["total"].sum())
    days_elapsed_year = (as_of - year_start).days + 1

    next_year_start = date(as_of.year + 1, 1, 1)
    days_in_year = (next_year_start - year_start).days

    projected_annual = (
        (ytd_total / days_elapsed_year) * days_in_year if days_elapsed_year > 0 else 0.0
    )

    return {
        "as_of": as_of,
        "yesterday_total": as_of_total,
        "projected_monthly": projected_monthly,
        "projected_annual": projected_annual,
    }


def month_actual_total(df, store_key, year, month):
    """Sum of actual (not projected) sales for one store for one completed
    calendar month, from whatever's in the log. Used for the monthly PDF
    report - unlike compute_pace, this is a plain sum with no projection
    math, since a past month is already finished."""
    store_df = df[df["store"] == store_key]
    month_df = store_df[
        (store_df["date"].apply(lambda d: d.year) == year)
        & (store_df["date"].apply(lambda d: d.month) == month)
    ]
    return float(month_df["total"].sum())

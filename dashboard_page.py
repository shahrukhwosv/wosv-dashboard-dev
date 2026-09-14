"""
Main Dashboard page - company-wide sales snapshot for yesterday, plus a
6-month trend.

Total Sales, Highest/Lowest Store, and the Monthly Trend chart all read
directly from the pace log Google Sheet (via sales_pace.read_daily_log()) -
the SAME sheet the Pace Calculator page uses, so this page never makes a
live Lightspeed API call itself.

Both that sheet and Mama's Sold (see below) are kept current by
nightly_refresh.py, meant to run once a night via a Railway Cron Job -
see that file for what it does and how to set it up. This page just
reads whatever nightly_refresh.py last saved; it does NOT fetch anything
live on page load, which is deliberate (nobody should have to wait on a
Lightspeed fetch just to open the homepage). If nobody's run that job
recently, numbers here can be stale - there's a caption below showing the
most recent date actually found in the sheet so that's visible at a
glance.

Mama's Sold specifically comes from dashboard_data.load_mama_snapshot() -
nightly_refresh.py computes it once a night (the pace log sheet only
tracks each store's total dollars per day, not a category/keyword
breakdown, so it can't be read from there like the other metrics; and a
live per-page-load fetch, which is what this page used to do, meant
every visit paid for a real Lightspeed round-trip across every store).
It's also the one metric that intentionally covers all connected stores
(unrestricted - the same scope Category Sales uses), not just the
Standard Stores list the rest of the page uses.

MTD Pace reuses sales_pace.compute_pace() (same projection math as the
Pace Calculator page) per store, summed into one company-wide projected
total for the current month.

Everything except Mama's Sold is restricted to the Standard Stores list,
same as Commissions/Transactions/Touch Tell/Monthly Reports/Purchase
Order Status.
"""

from datetime import date, timedelta

import streamlit as st
import pandas as pd

import lightspeed_client as ls
from store_access import get_page_store_keys, STANDARD_STORES_LIST, DEFAULT_STANDARD_STORE_KEYS
from sales_pace import read_daily_log, compute_pace, store_local_today
from dashboard_data import load_mama_snapshot

st.title("Dashboard")

config = ls.load_config()
stores = config["stores"]

store_keys = list(get_page_store_keys(
    config, list_name=STANDARD_STORES_LIST, default_keys=DEFAULT_STANDARD_STORE_KEYS
))
store_names = {key: stores[key].get("name", key) for key in store_keys}

if not store_keys:
    st.info("No stores available. Check the Standard Stores list on the Manage Users page.")
    st.stop()

log_df = read_daily_log()
accessible_log_df = log_df[log_df["store"].isin(store_keys)]

if accessible_log_df.empty:
    st.warning(
        "No data found in the pace log sheet yet for your stores. Visit "
        "the Pace Calculator page and click 'Fetch missing days from "
        "Lightspeed' to populate it."
    )
    st.stop()

most_recent_date = accessible_log_df["date"].max()
yesterday = store_local_today() - timedelta(days=1)
if most_recent_date < yesterday:
    st.warning(
        f"⚠️ The pace log sheet's most recent data is from "
        f"{most_recent_date.strftime('%B %d')} - it looks like nobody has "
        f"clicked 'Fetch missing days from Lightspeed' on the Pace "
        f"Calculator page recently. Numbers below may be out of date."
    )

# --- Yesterday's totals (or the most recent day actually in the sheet) ---
snapshot_date = min(yesterday, most_recent_date)
day_df = accessible_log_df[accessible_log_df["date"] == snapshot_date]
total_sales = float(day_df["total"].sum())

highest_row = day_df.loc[day_df["total"].idxmax()] if not day_df.empty else None
lowest_row = day_df.loc[day_df["total"].idxmin()] if not day_df.empty else None

st.caption(f"Showing {snapshot_date.strftime('%A, %B %d')} across {len(store_keys)} store(s)")

# Mama's Sold no longer fetches live - see nightly_refresh.py, which
# computes this once a night and saves it here via dashboard_data.py.
mama_date, mama_total, mama_quantity, mama_failed_stores, mama_updated_at = load_mama_snapshot()

if mama_total is None:
    st.caption(
        "⚠️ Mama's Sold hasn't been computed yet - the nightly refresh job "
        "hasn't run for the first time. See nightly_refresh.py for setup."
    )
    mama_total, mama_quantity = 0.0, 0.0
elif mama_failed_stores:
    failed_names = ", ".join(stores[k].get("name", k) for k in mama_failed_stores if k in stores)
    st.caption(f"⚠️ Mama's Sold couldn't be fetched for: {failed_names} - excluded from the total below.")

# --- Month-to-date pace, company-wide ---
today = store_local_today()
mtd_projected_total = sum(
    compute_pace(accessible_log_df, store_key, today)["projected_monthly"]
    for store_key in store_keys
)

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total Sales", f"${total_sales:,.2f}")
col2.metric(
    "Highest Store",
    store_names.get(highest_row["store"], highest_row["store"]) if highest_row is not None else "—",
    f"${highest_row['total']:,.2f}" if highest_row is not None else None,
)
col3.metric(
    "Lowest Store",
    store_names.get(lowest_row["store"], lowest_row["store"]) if lowest_row is not None else "—",
    f"${lowest_row['total']:,.2f}" if lowest_row is not None else None,
)
mama_delta = f"${mama_total:,.2f}"
if mama_date:
    mama_delta += f" ({mama_date.strftime('%b %d')})"
col4.metric("Mama's Sold", f"{mama_quantity:,.0f} units", mama_delta)
col5.metric("MTD Pace", f"${mtd_projected_total:,.0f}", "Projected this month")


# --- Monthly trend chart ---
st.divider()
st.subheader("Monthly Sales Trend")

MONTHS_BACK = 6
year, month = today.year, today.month
for _ in range(MONTHS_BACK - 1):
    month -= 1
    if month == 0:
        month = 12
        year -= 1
earliest_start = date(year, month, 1)

month_order = []
y, m = year, month
for _ in range(MONTHS_BACK):
    month_order.append(date(y, m, 1).strftime("%b %Y"))
    m += 1
    if m == 13:
        m = 1
        y += 1

trend_df = accessible_log_df[accessible_log_df["date"] >= earliest_start].copy()

if trend_df.empty:
    st.write("No historical data in the pace log sheet yet for this range.")
else:
    store_names = {k: stores[k].get("name", k) for k in store_keys}

    selected_names = st.multiselect(
        "Stores to show on the chart",
        options=sorted(store_names.values()),
        default=sorted(store_names.values()),
    )

    trend_df["month"] = trend_df["date"].apply(lambda d: d.strftime("%b %Y"))
    trend_df["store_name"] = trend_df["store"].map(store_names)
    trend_df = trend_df[trend_df["store_name"].isin(selected_names)]

    if trend_df.empty:
        st.write("No stores selected.")
    else:
        pivot = trend_df.pivot_table(
            index="month", columns="store_name", values="total", aggfunc="sum"
        )
        pivot = pivot.reindex(month_order)
        st.line_chart(pivot)

"""
Main Dashboard page - company-wide sales snapshot.

Total Sales, Average Sales per Store, and the Monthly Trend chart all read
directly from the pace log Google Sheet (via sales_pace.read_daily_log()) -
the SAME sheet the Pace Calculator page uses. This means the Dashboard
itself never makes a Lightspeed API call - it's purely reading whatever's
already in the sheet.

IMPORTANT: that sheet only updates when someone clicks "Fetch missing days
from Lightspeed" on the Pace Calculator page (a manual button, not
automatic/scheduled). If nobody's clicked it in a while, these numbers can
be stale or missing recent days - there's a caption below showing the most
recent date actually found in the sheet so that's visible at a glance.

Average Units per Sale is the one exception - the sheet only tracks dollar
totals, not units, so that metric still reads from the daily_store_sales
database table (see sales_summary.py / sync_daily_sales.py), which DOES
require a real Lightspeed sync to populate.
"""

from datetime import date, timedelta

import streamlit as st
import pandas as pd

from lightspeed_client import load_config
from store_access import get_accessible_store_keys
from sales_summary import get_daily_metrics
from sales_pace import read_daily_log

st.title("Dashboard")

config = load_config()
stores = config["stores"]

if st.session_state.get("is_admin"):
    accessible_store_keys = [
        key for key, val in stores.items() if val.get("refresh_token")
    ]
else:
    granted = get_accessible_store_keys(st.session_state.user_id)
    accessible_store_keys = [
        key for key, val in stores.items()
        if val.get("refresh_token") and key in granted
    ]

if not accessible_store_keys:
    st.info('No stores added. Click "Add a Store" in the menu to connect new stores.')
    st.stop()

log_df = read_daily_log()
accessible_log_df = log_df[log_df["store"].isin(accessible_store_keys)]

if accessible_log_df.empty:
    st.warning(
        "No data found in the pace log sheet yet for your stores. Visit "
        "the Pace Calculator page and click 'Fetch missing days from "
        "Lightspeed' to populate it."
    )
    st.stop()

most_recent_date = accessible_log_df["date"].max()
yesterday = date.today() - timedelta(days=1)
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
store_count = len(accessible_store_keys)
avg_sales_per_store = total_sales / store_count if store_count else 0

# Units still requires a real Lightspeed sync - the sheet doesn't track it.
_, total_units, total_sale_count = get_daily_metrics(accessible_store_keys, snapshot_date)
avg_units_per_sale = total_units / total_sale_count if total_sale_count else 0

st.caption(f"Showing {snapshot_date.strftime('%A, %B %d')} across {store_count} store(s)")

col1, col2, col3 = st.columns(3)
col1.metric("Total Sales", f"${total_sales:,.2f}")
col2.metric("Average Sales per Store", f"${avg_sales_per_store:,.2f}")
col3.metric("Average Units per Sale", f"{avg_units_per_sale:,.2f}")
if total_sale_count == 0:
    st.caption(
        "Units per sale needs a real Lightspeed sync (sync_daily_sales.py) "
        "for this date - the pace sheet only tracks dollar totals."
    )


# --- Monthly trend chart ---
st.divider()
st.subheader("Monthly Sales Trend")

MONTHS_BACK = 6
today = date.today()
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
    store_names = {k: stores[k].get("name", k) for k in accessible_store_keys}

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

"""
Main Dashboard page - company-wide sales snapshot.

Reads from the daily_store_sales table (see sales_summary.py), which is
kept up to date by sync_daily_sales.py running nightly via Railway Cron.
This means the dashboard loads instantly regardless of how many stores
there are, instead of making live Lightspeed API calls on every visit.

Shows:
  1. Total sales across accessible stores, yesterday
  2. Average sales per store, yesterday
  3. Average units per sale, yesterday
  4. A monthly sales trend chart, one colored line per store

Scoped to whichever stores the logged-in user can see (all stores for
admins, granted stores only for everyone else - same access rule as every
other page).
"""

from datetime import date, timedelta

import streamlit as st
import pandas as pd

from lightspeed_client import load_config
from store_access import get_accessible_store_keys
from sales_summary import get_daily_metrics, get_monthly_trend

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

yesterday = date.today() - timedelta(days=1)
total_sales, total_units, total_sale_count = get_daily_metrics(
    accessible_store_keys, yesterday
)

store_count = len(accessible_store_keys)
avg_sales_per_store = total_sales / store_count if store_count else 0
avg_units_per_sale = total_units / total_sale_count if total_sale_count else 0

st.caption(f"Showing {yesterday.strftime('%A, %B %d')} across {store_count} store(s)")

col1, col2, col3 = st.columns(3)
col1.metric("Total Sales (Yesterday)", f"${total_sales:,.2f}")
col2.metric("Average Sales per Store", f"${avg_sales_per_store:,.2f}")
col3.metric("Average Units per Sale", f"{avg_units_per_sale:,.2f}")


st.divider()
st.subheader("Monthly Sales Trend")

MONTHS_BACK = 6

records, month_order = get_monthly_trend(accessible_store_keys, MONTHS_BACK, date.today())

if not records:
    st.write(
        "No historical data yet - run the initial backfill "
        "(sync_daily_sales.py --backfill-days N) to populate this chart."
    )
else:
    store_names = {k: stores[k].get("name", k) for k in accessible_store_keys}
    trend_df = pd.DataFrame(records, columns=["store_key", "month", "sales"])
    trend_df["store"] = trend_df["store_key"].map(store_names)

    pivot = trend_df.pivot_table(index="month", columns="store", values="sales", aggfunc="sum")
    pivot = pivot.reindex(month_order)

    st.line_chart(pivot)

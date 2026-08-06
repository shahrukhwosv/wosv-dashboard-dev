"""
Main Dashboard page - company-wide sales snapshot.

Shows three headline numbers for YESTERDAY, scoped to whichever stores the
logged-in user can see (all stores for admins, granted stores only for
everyone else - same access rule as every other page):

  1. Total sales across all those stores
  2. Average sales per store (total / number of stores)
  3. Average units per sale (total items sold / total number of sales)
"""

from datetime import date, timedelta

import streamlit as st

from lightspeed_client import load_config, fetch_sales
from store_access import get_accessible_store_keys

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


def _sale_units(sale):
    lines = sale.get("SaleLines", {}).get("SaleLine", [])
    if isinstance(lines, dict):
        lines = [lines]
    return sum(abs(float(line.get("unitQuantity", 1) or 1)) for line in lines)


@st.cache_data(ttl=900, show_spinner=False)  # 15 min - avoids re-hitting the Lightspeed API on every page view
def _fetch_yesterday_metrics(store_keys):
    yesterday = date.today() - timedelta(days=1)
    total_sales = 0.0
    total_units = 0.0
    total_sale_count = 0

    for store_key in store_keys:
        raw_sales = fetch_sales(config, store_key, yesterday, yesterday)
        for sale in raw_sales:
            total_sales += float(sale.get("total", sale.get("calcTotal", 0)) or 0)
            total_units += _sale_units(sale)
            total_sale_count += 1

    return total_sales, total_units, total_sale_count, yesterday


with st.spinner("Pulling yesterday's sales..."):
    total_sales, total_units, total_sale_count, yesterday = _fetch_yesterday_metrics(
        tuple(sorted(accessible_store_keys))
    )

store_count = len(accessible_store_keys)
avg_sales_per_store = total_sales / store_count if store_count else 0
avg_units_per_sale = total_units / total_sale_count if total_sale_count else 0

st.caption(f"Showing {yesterday.strftime('%A, %B %d')} across {store_count} store(s)")

col1, col2, col3 = st.columns(3)
col1.metric("Total Sales (Yesterday)", f"${total_sales:,.2f}")
col2.metric("Average Sales per Store", f"${avg_sales_per_store:,.2f}")
col3.metric("Average Units per Sale", f"{avg_units_per_sale:,.2f}")

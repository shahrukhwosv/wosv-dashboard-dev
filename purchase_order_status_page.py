"""
Purchase Order Status page (purchase_order_status_page.py).

Built to catch two things that were previously invisible without opening
every store's PO list one at a time:
  - a PO sitting in "Ordered" a long time (placed with the vendor, not
    yet received - or received but never logged as such)
  - a PO sitting in "Check In" a long time (received, but the district
    manager's final review/count to mark it Finished hasn't happened)

No automatic flagging or thresholds - just sortable "Days Since Ordered"
/ "Days Since Received" columns, so the oldest ones surface by sorting
the table rather than a fixed cutoff. Click a column header to sort.

Context: district managers now create the PO in Lightspeed themselves,
at the moment they place an order with a vendor (not after it arrives) -
see purchase_order_status.py's docstring for what that changes about
which fields mean what. Vendors' own order/invoice numbers don't always
match Lightspeed's own PO reference number, so DMs record the vendor's
number in the PO's notes field - that's included here as its own column
so it's searchable without opening Lightspeed.

Restricted to the Standard Stores list, same as Commissions/Transactions/
Touch Tell/Monthly Reports.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import streamlit as st

import lightspeed_client as ls
from purchase_order_status import fetch_purchase_order_status, fetch_vendor_lookup
from sales_pace import store_local_today
from store_access import get_page_store_keys, STANDARD_STORES_LIST, DEFAULT_STANDARD_STORE_KEYS

STAGE_LABELS = {
    "open": "Open",
    "ordered": "Ordered",
    "check_in": "Check In",
    "finished": "Finished",
}

st.title("Purchase Order Status")
st.caption(
    "Every purchase order across your stores, so a delayed entry or a "
    "stalled review is visible at a glance. Sort by the Days Since "
    "columns (click a column header) to see what's oldest."
)


def _parse_date(value):
    """Lightspeed date/time fields come back as ISO strings - returns a
    plain date, or None if missing/unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


config = ls.load_config()
store_keys = sorted(
    get_page_store_keys(config, list_name=STANDARD_STORES_LIST, default_keys=DEFAULT_STANDARD_STORE_KEYS),
    key=lambda key: (config["stores"][key].get("name") or key).casefold(),
)
store_names = {key: config["stores"][key].get("name", key) for key in store_keys}

if not store_keys:
    st.info("No stores available. Check the Standard Stores list on the Manage Users page.")
    st.stop()

show_finished = st.checkbox("Also show Finished POs", value=False)

if st.button("Load purchase orders", type="primary"):
    with st.spinner(f"Fetching purchase orders for {len(store_keys)} store(s)..."):
        def _fetch_one(store_key):
            vendor_lookup = fetch_vendor_lookup(config, store_key)
            pos = fetch_purchase_order_status(config, store_key)
            return store_key, vendor_lookup, pos

        all_rows = []
        with ThreadPoolExecutor(max_workers=max(len(store_keys), 1)) as pool:
            futures = [pool.submit(_fetch_one, store_key) for store_key in store_keys]
            for future in as_completed(futures):
                store_key, vendor_lookup, pos = future.result()
                today = store_local_today()
                for po in pos:
                    ordered_date = _parse_date(po["ordered_date"])
                    received_date = _parse_date(po["received_date"])
                    created_date = _parse_date(po["create_time"])

                    all_rows.append({
                        "Store": store_names.get(store_key, store_key),
                        "Vendor": vendor_lookup.get(po["vendor_id"], "(unknown)"),
                        "Stage": STAGE_LABELS.get(po["stage"], po["stage"]),
                        "PO #": po["reference_number"],
                        "Vendor Order #": po["notes"],
                        "Created": created_date,
                        "Days Since Created": (today - created_date).days if created_date else None,
                        "Ordered": ordered_date,
                        "Days Since Ordered": (today - ordered_date).days if ordered_date else None,
                        "Received": received_date,
                        "Days Since Received": (today - received_date).days if received_date else None,
                        "Total": po["total"],
                    })

        st.session_state["po_status_rows"] = all_rows

rows = st.session_state.get("po_status_rows")
if rows is None:
    st.info("Click \"Load purchase orders\" to fetch current data.")
    st.stop()

df = pd.DataFrame(rows)
if not show_finished:
    df = df[df["Stage"] != "Finished"]

if df.empty:
    st.write("No purchase orders match the current filter.")
else:
    st.dataframe(
        df,
        column_config={
            "Total": st.column_config.NumberColumn(format="$%.2f"),
            "Days Since Created": st.column_config.NumberColumn(format="%d"),
            "Days Since Ordered": st.column_config.NumberColumn(format="%d"),
            "Days Since Received": st.column_config.NumberColumn(format="%d"),
        },
        hide_index=True,
        use_container_width=True,
    )

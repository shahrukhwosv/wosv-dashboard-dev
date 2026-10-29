"""
WOSV Lost Sales page.

WOSV-brand items lost on Top Shelf sales orders this year (ordered but not
shipped on that order), split into "Still owed" and "Fulfilled since".
A lost line clears automatically once a later invoice sends the same item
to the same store - see wosv_lost_sales.py for the exact rule.

Reads live from the ERP (cached 15 minutes; "Refresh from ERP" re-pulls).
Uses the same ERP login as the Top Shelf Invoices page - if it isn't
connected, an admin connects it there.
"""
import pandas as pd
import requests
import streamlit as st

import topshelf_erp
import wosv_lost_sales
from sales_pace import store_local_today

st.title("WOSV Lost Sales")

today = store_local_today()
st.caption(
    f"WOSV-brand items ordered on a Top Shelf sales order since Jan 1, {today.year} that didn't ship on that order. "
    "A line clears on its own once a later invoice sends the same item to the same store."
)


@st.cache_data(ttl=900, show_spinner=False)
def _load(day):
    lines = wosv_lost_sales.fetch_lines(today=day)
    return wosv_lost_sales.to_tables(lines, today=day)


if st.button("Refresh from ERP"):
    _load.clear()

try:
    with st.spinner("Pulling lost sales from the ERP..."):
        open_df, done_df = _load(today)
except topshelf_erp.ErpAuthError as e:
    st.error(f"{e}")
    st.stop()
except requests.HTTPError as e:
    code = e.response.status_code if e.response is not None else "?"
    if code in (401, 403):
        try:
            token_info = topshelf_erp.ErpClient().token_summary()
        except Exception as te:
            token_info = f"couldn't read token ({te})"
        st.error(
            f"The ERP refused the request (HTTP {code}), even with a fresh login token.\n\n"
            f"ERP said: {e}\n\nDashboard's ERP login: {token_info}\n\n"
            "If you just changed this ERP user's access, reconnect the ERP from the Top Shelf "
            "Invoices page so the dashboard signs in again."
        )
    else:
        st.error(f"ERP request failed (HTTP {code}): {e}")
    st.stop()
except Exception as e:
    st.error(f"Couldn't load lost sales: {e}")
    st.stop()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

owed_units = open_df["Still Owed"].sum() if not open_df.empty else 0
owed_value = open_df["Owed Value"].sum() if not open_df.empty else 0
m1, m2, m3, m4 = st.columns(4)
m1.metric("Lines still owed", f"{len(open_df):,}")
m2.metric("Units still owed", f"{owed_units:,.0f}")
m3.metric("Value still owed", f"${owed_value:,.2f}")
m4.metric("Fulfilled since", f"{len(done_df):,}")

qty_fmt = st.column_config.NumberColumn(format="%d")
common_config = {
    "Order Date": st.column_config.DateColumn("Order Date", format="MM/DD/YYYY", help="When the original sales order was created"),
    "Sales Order #": st.column_config.NumberColumn(format="%d", help="The original sales order the item was lost on"),
    "Lost Qty": qty_fmt,
}


# ---------------------------------------------------------------------------
# Still owed
# ---------------------------------------------------------------------------

st.subheader("Still owed")
if open_df.empty:
    st.success("Nothing owed - every WOSV lost item this year has been sent.")
else:
    stores = sorted(open_df["Store"].dropna().unique())
    picked_stores = st.multiselect("Stores", stores, placeholder="All stores")
    show = open_df[open_df["Store"].isin(picked_stores)] if picked_stores else open_df

    cols = ["Order Date", "Sales Order #", "Store", "Product", "Still Owed", "Days Waiting", "In Stock Now",
            "Owed Value", "Lost Qty", "Order Status", "Partly Sent On", "Customer", "Rep", "SKU"]
    st.dataframe(
        show[cols],
        hide_index=True,
        width="stretch",
        column_config={
            **common_config,
            "Still Owed": st.column_config.NumberColumn(format="%d", help="Lost qty minus anything sent on later invoices"),
            "Days Waiting": st.column_config.NumberColumn(format="%d", help="Days since the original order"),
            "In Stock Now": st.column_config.NumberColumn(format="%d", help="Available in the ERP right now"),
            "Owed Value": st.column_config.NumberColumn(format="dollar", help="Still owed x the price on the original order"),
            "Order Status": st.column_config.TextColumn(help="Status of the original sales order in the ERP"),
            "Partly Sent On": st.column_config.TextColumn(help="Later invoices that covered part of this line"),
        },
    )

    can_ship = show[pd.to_numeric(show["In Stock Now"], errors="coerce").fillna(0) >= show["Still Owed"]]
    if not can_ship.empty:
        st.caption(f"{len(can_ship)} of these have enough stock on hand to ship now.")


# ---------------------------------------------------------------------------
# Fulfilled
# ---------------------------------------------------------------------------

with st.expander(f"Fulfilled since ({len(done_df)})"):
    if done_df.empty:
        st.write("None yet.")
    else:
        st.dataframe(
            done_df[["Order Date", "Sales Order #", "Store", "Product", "Lost Qty", "Fulfilled On",
                     "Invoice #", "Days To Fulfill", "Value", "Customer", "SKU"]],
            hide_index=True,
            width="stretch",
            column_config={
                **common_config,
                "Fulfilled On": st.column_config.DateColumn(format="MM/DD/YYYY", help="Date of the invoice that finished covering it"),
                "Invoice #": st.column_config.TextColumn(help="Later invoice(s) that sent the item"),
                "Days To Fulfill": st.column_config.NumberColumn(format="%d"),
                "Value": st.column_config.NumberColumn(format="dollar"),
            },
        )


# ---------------------------------------------------------------------------
# Download + how it works
# ---------------------------------------------------------------------------

if not open_df.empty or not done_df.empty:
    export = pd.concat(
        [open_df.assign(Status="Still owed"), done_df.assign(Status="Fulfilled")],
        ignore_index=True,
    )
    st.download_button(
        "Download CSV",
        export.to_csv(index=False).encode(),
        file_name=f"wosv_lost_sales_{today.isoformat()}.csv",
        mime="text/csv",
    )

with st.expander("How this works"):
    st.markdown(
        "- **Lost lines** come from the ERP's *Report > Lost Sales* (by product), filtered to the **WOSV** brand, "
        f"for sales orders created since Jan 1, {today.year}. An item only appears here once its Brand is set to WOSV "
        "on the product in the ERP.\n"
        "- A line counts as **fulfilled** when a later invoice sends the **same product** to the **same store**. "
        "The original order's own invoice never counts. Quantities are applied oldest lost line first, so "
        "a partial shipment leaves the rest still owed. A store's normal reorder of the item counts too.\n"
        "- If the original order is later invoiced in full on the same sales order, the ERP drops it from Lost "
        "Sales, and it drops off this page too.\n"
        "- Data is live from the ERP, cached for 15 minutes. Use **Refresh from ERP** after sending something."
    )

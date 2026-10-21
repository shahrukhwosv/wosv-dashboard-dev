"""
Doordash Sales page (doordash_sales_page.py).

Shows how many sales (and how much money) went through the "DoorDash"
customer at each store for a date range - defaults to today.

The lookup itself lives in doordash_sales.py (shared with the nightly
refresh that feeds the morning brief). How it works, per store (stores
run in parallel, same as Category Sales):
  1. Look up the store's customer record(s) whose first name, last name
     or company matches the search text (default "doordash"). The match
     is a wildcard on the letters with anything in between, so "DoorDash",
     "Door Dash", "DOORDASH ORDER" etc. all match. Customers are
     per-account in Lightspeed, so each store has its own DoorDash
     customerID - that's why this lookup runs at every store.
  2. Pull only that customer's completed sales in the date range
     (customerID filter on Sale.json), instead of every sale at the store.
  Voided/archived sales are dropped, same as fetch_sales().

The customer records matched at each store are listed under the results,
so it's easy to spot a store where the customer is named differently
(e.g. "DD Orders") - just change the search text.

"Today" is computed in Central time, not the server's clock - Railway
runs in UTC, so after ~7 PM Central date.today() would already be
tomorrow and the Today button would come back empty.

Respects per-user store access via get_page_store_keys (no store list
restriction), and page access via page key "doordash_sales".
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import doordash_sales as dd
import lightspeed_client as ls
from store_access import get_lm_store_keys, get_page_store_keys

st.title("Doordash Sales")

CENTRAL = ZoneInfo("America/Chicago")


def _today_central():
    return datetime.now(CENTRAL).date()


def _sale_row(sale, store_name, store_key, customers):
    store_tz = ls._get_store_timezone(store_key)
    raw_time = sale.get("completeTime") or sale.get("timeStamp") or ""
    try:
        local_time = datetime.fromisoformat(raw_time).astimezone(store_tz)
        time_label = local_time.strftime("%b %d, %I:%M %p")
        sort_time = local_time.timestamp()
    except ValueError:
        time_label, sort_time = raw_time, 0

    items = []
    for line in dd.as_list((sale.get("SaleLines") or {}).get("SaleLine")):
        qty = dd.num(line.get("unitQuantity"))
        description = ((line.get("Item") or {}).get("description") or "").strip() or "Misc item"
        items.append(f"{qty:g} × {description}" if qty != 1 else description)

    subtotal = dd.num(sale.get("calcSubtotal")) - abs(dd.num(sale.get("calcDiscount")))
    tax = dd.num(sale.get("calcTax1")) + dd.num(sale.get("calcTax2"))

    return {
        "_sort": sort_time,
        "Store": store_name,
        "Time": time_label,
        "Ticket #": sale.get("ticketNumber") or sale.get("saleID"),
        "Customer": customers.get(str(sale.get("customerID")), ""),
        "Items": ", ".join(items),
        "Subtotal": subtotal,
        "Tax": tax,
        "Total": dd.sale_total(sale),
    }


def run_report(store_keys, store_names, search_text, start_date, end_date):
    """Lookup + retries live in doordash_sales.fetch_stores (shared with
    the nightly refresh / morning brief); this just turns raw sales into rows."""
    raw = dd.fetch_stores(store_keys, start_date, end_date, search_text)
    return {
        key: {
            "customers": r["customers"],
            "rows": [_sale_row(s, store_names[key], key, r["customers"]) for s in r["sales"]],
            "error": r["error"],
        }
        for key, r in raw.items()
    }



def render_report(report):
    results = report["results"]
    store_names = report["store_names"]
    selected = report["selected_stores"]

    all_rows = [row for key in selected for row in results[key]["rows"]]
    total_count = len(all_rows)
    total_amount = sum(row["Total"] for row in all_rows)

    if report["start_date"] == report["end_date"]:
        period = report["start_date"].strftime("%a %b %d, %Y")
    else:
        period = f"{report['start_date']:%b %d} – {report['end_date']:%b %d, %Y}"
    st.caption(f"Customer search “{report['search_text']}” · {period}")

    col1, col2, col3 = st.columns(3)
    col1.metric("Sales", f"{total_count:,}")
    col2.metric("Total (incl. tax)", f"${total_amount:,.2f}")
    col3.metric("Stores with sales", sum(1 for key in selected if results[key]["rows"]))

    errors = [key for key in selected if results[key]["error"]]
    if errors:
        st.warning(
            f"Couldn't get data from Lightspeed for: {', '.join(store_names[k] for k in errors)}. "
            "These stores aren't included in the totals below."
        )
        with st.expander("Error details"):
            for key in errors:
                st.markdown(f"**{store_names[key]}**")
                st.code(results[key]["error"][:1500], language=None)

    no_customer = [
        store_names[key] for key in selected
        if not results[key]["customers"] and not results[key]["error"]
    ]

    summary = pd.DataFrame([
        {
            "Store": store_names[key],
            "Sales": len(results[key]["rows"]),
            "Total": sum(row["Total"] for row in results[key]["rows"]),
        }
        for key in selected
        if results[key]["rows"]
    ])

    if summary.empty:
        st.info("No sales to this customer for the selected stores and dates.")
    else:
        st.subheader("By store")
        st.dataframe(
            summary.sort_values("Total", ascending=False),
            hide_index=True,
            column_config={"Total": st.column_config.NumberColumn(format="$%.2f")},
        )

        st.subheader("Sales")
        detail = pd.DataFrame(all_rows).sort_values("_sort", ascending=False).drop(columns="_sort")
        st.dataframe(
            detail,
            hide_index=True,
            width="stretch",
            column_config={
                "Subtotal": st.column_config.NumberColumn(format="$%.2f"),
                "Tax": st.column_config.NumberColumn(format="$%.2f"),
                "Total": st.column_config.NumberColumn(format="$%.2f"),
                "Items": st.column_config.TextColumn(width="large"),
            },
        )
        st.download_button(
            "Download CSV",
            detail.to_csv(index=False).encode("utf-8"),
            file_name=f"doordash_sales_{report['start_date']}_{report['end_date']}.csv",
            mime="text/csv",
        )

    with st.expander("Customer records matched at each store"):
        for key in selected:
            customers = results[key]["customers"]
            label = ", ".join(sorted(set(customers.values()), key=str.casefold)) if customers else "— none found —"
            st.markdown(f"**{store_names[key]}:** {label}")
        if no_customer:
            st.caption(
                "Stores showing “none found” have no customer matching the search text - "
                "if DoorDash is set up under a different name there, change the search above."
            )


# ----------------------------------------------------------------------
# Page controls
# ----------------------------------------------------------------------

config = ls.load_config()
stores = config["stores"]
visible_keys = get_page_store_keys(config)
connected_stores = {key: val for key, val in stores.items() if key in visible_keys}

if not connected_stores:
    st.info('No stores added. Click "Add a Store" in the menu to connect new stores.')
    st.stop()

def _store_name(key):
    return connected_stores[key].get("name") or key

all_store_keys = sorted(connected_stores.keys(), key=lambda key: _store_name(key).casefold())

search_text = st.text_input(
    "Customer name",
    value="doordash",
    help="Matches the customer's first name, last name or company. Spaces and letter case don't matter, so “doordash” also finds “Door Dash”.",
).strip()

store_scope = st.radio("Stores", ["All stores", "LM Stores", "Choose stores"], horizontal=True, key="dd_store_scope")
if store_scope == "All stores":
    selected_stores = all_store_keys
elif store_scope == "LM Stores":
    # Every Pace Calculator store this user can see
    lm_keys = get_lm_store_keys(config)
    selected_stores = [key for key in all_store_keys if key in lm_keys]
else:
    selected_stores = sorted(
        st.multiselect("Select stores", all_store_keys, default=all_store_keys, format_func=_store_name),
        key=lambda key: _store_name(key).casefold(),
    )

today = _today_central()
if "dd_start" not in st.session_state:
    st.session_state["dd_start"] = today
    st.session_state["dd_end"] = today

preset_cols = st.columns(4)
presets = {
    "Today": (today, today),
    "Yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
    "This Week": (today - timedelta(days=today.weekday()), today),
    "This Month": (today.replace(day=1), today),
}
for col, (label, (start, end)) in zip(preset_cols, presets.items()):
    if col.button(label, width="stretch", key=f"dd_preset_{label}"):
        st.session_state["dd_start"] = start
        st.session_state["dd_end"] = end

col1, col2 = st.columns(2)
with col1:
    start_date = st.date_input("Start date", key="dd_start")
with col2:
    end_date = st.date_input("End date", key="dd_end")

if start_date > end_date:
    st.error("Start date must be before end date.")
    st.stop()
if not selected_stores:
    st.info("Pick at least one store to run the report.")
    st.stop()
if not search_text:
    st.info("Enter a customer name to run the report.")
    st.stop()

if st.button("Run report", type="primary"):
    store_names = {key: _store_name(key) for key in selected_stores}
    with st.spinner(f"Checking {len(selected_stores)} store(s)..."):
        results = run_report(
            selected_stores, store_names, search_text, start_date, end_date
        )
    st.session_state["dd_report"] = {
        "results": results,
        "store_names": store_names,
        "selected_stores": selected_stores,
        "search_text": search_text,
        "start_date": start_date,
        "end_date": end_date,
    }

report = st.session_state.get("dd_report")
if report:
    render_report(report)

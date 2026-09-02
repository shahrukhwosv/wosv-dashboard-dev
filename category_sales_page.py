"""
Category Sales page (category_sales_page.py).

Lets the user search sales three ways:
  - Category: pick a category (e.g. "Rolling Trays") from a dropdown
  - Keyword: type a keyword (e.g. "mug") and it matches any item whose
    description contains that word
  - UPC: type an exact UPC and it matches the one item with that code

Either way, pick a date range and which stores to include, and it shows
how much each selected store sold for that search over that period, plus
an expandable per-store breakdown of exactly which items contributed to
that total.

Respects per-user store access (see store_access.py): admins see every
connected store, regular users only see stores explicitly granted to them -
same pattern as commission_page.py.

NOTE: Categories are per-account in Lightspeed, so the same category name
can have a different categoryID at each store. This page builds the
dropdown from category *names* merged across all stores, then resolves the
right categoryID for each store individually before pulling sales. Keyword
search doesn't have this problem - it's just a description match run
independently at each store. Stores are queried in parallel (each store is
an independent Lightspeed account, so there's no shared state to worry
about).
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import lightspeed_client as ls
from store_access import get_accessible_store_keys

st.title("Category Sales")


@st.cache_data(ttl=3600, show_spinner=False)
def get_categories_by_store(store_keys):
    """Returns {store_key: {categoryID: name}} for every store."""
    config = ls.load_config()
    return {
        store_key: ls.fetch_categories(config, store_key)
        for store_key in store_keys
    }


def build_name_options(categories_by_store):
    """Merges categories from every store into a sorted list of unique names."""
    names = set()
    for categories in categories_by_store.values():
        names.update(categories.values())
    return sorted(names, key=str.casefold)


def run_category_report(store_keys, categories_by_store, selected_category, start_date, end_date):
    """Fetches category sales for each store in parallel."""
    config = ls.load_config()
    results = {}

    def _fetch_one(store_key):
        store_categories = categories_by_store[store_key]
        category_id = next(
            (cid for cid, name in store_categories.items() if name == selected_category),
            None,
        )
        if category_id is None:
            return store_key, None  # no matching category name at this store
        return store_key, ls.fetch_category_sales(
            config, store_key, category_id, start_date, end_date
        )

    with ThreadPoolExecutor(max_workers=max(len(store_keys), 1)) as pool:
        futures = [pool.submit(_fetch_one, store_key) for store_key in store_keys]
        for future in as_completed(futures):
            store_key, result = future.result()
            results[store_key] = result

    return results


def run_keyword_report(store_keys, keyword, start_date, end_date):
    """Fetches keyword sales for each store in parallel."""
    config = ls.load_config()
    results = {}

    def _fetch_one(store_key):
        return store_key, ls.fetch_keyword_sales(config, store_key, keyword, start_date, end_date)

    with ThreadPoolExecutor(max_workers=max(len(store_keys), 1)) as pool:
        futures = [pool.submit(_fetch_one, store_key) for store_key in store_keys]
        for future in as_completed(futures):
            store_key, result = future.result()
            results[store_key] = result

    return results


def run_upc_report(store_keys, upc, start_date, end_date):
    """Fetches UPC sales for each store in parallel."""
    config = ls.load_config()
    results = {}

    def _fetch_one(store_key):
        return store_key, ls.fetch_upc_sales(config, store_key, upc, start_date, end_date)

    with ThreadPoolExecutor(max_workers=max(len(store_keys), 1)) as pool:
        futures = [pool.submit(_fetch_one, store_key) for store_key in store_keys]
        for future in as_completed(futures):
            store_key, result = future.result()
            results[store_key] = result

    return results


config = ls.load_config()
stores = config["stores"]

if st.session_state.get("is_admin"):
    connected_stores = {
        key: val for key, val in stores.items()
        if val.get("refresh_token") and not val.get("pace_only")
    }
else:
    accessible_keys = get_accessible_store_keys(st.session_state.user_id)
    connected_stores = {
        key: val for key, val in stores.items()
        if val.get("refresh_token")
        and not val.get("pace_only")
        and key in accessible_keys
    }

if not connected_stores:
    st.info('No stores added. Click "Add a Store" in the menu to connect new stores.')
    st.stop()

all_store_keys = list(connected_stores.keys())

search_mode = st.radio("Search by", ["Category", "Keyword", "UPC"], horizontal=True)

selected_category = None
keyword = None
upc = None

if search_mode == "Category":
    with st.spinner("Loading categories..."):
        categories_by_store = get_categories_by_store(tuple(all_store_keys))

    category_options = build_name_options(categories_by_store)

    if not category_options:
        st.warning("No categories found for your accessible stores.")
        st.stop()

    selected_category = st.selectbox("Category", category_options)
elif search_mode == "Keyword":
    keyword = st.text_input("Keyword", placeholder="e.g. mug").strip()
else:
    upc = st.text_input("UPC", placeholder="e.g. 012345678905").strip()

store_scope = st.radio("Stores", ["All stores", "Choose stores"], horizontal=True)
if store_scope == "All stores":
    selected_stores = all_store_keys
else:
    selected_stores = st.multiselect(
        "Select stores",
        all_store_keys,
        default=all_store_keys,
        format_func=lambda store_key: connected_stores[store_key].get("name") or store_key,
    )

col1, col2 = st.columns(2)
with col1:
    start_date = st.date_input("Start date", value=date.today() - timedelta(days=30))
with col2:
    end_date = st.date_input("End date", value=date.today())

if start_date > end_date:
    st.error("Start date must be before end date.")
    st.stop()

if not selected_stores:
    st.info("Pick at least one store to run the report.")
    st.stop()

if search_mode == "Keyword" and not keyword:
    st.info("Enter a keyword to run the report.")
    st.stop()

if search_mode == "UPC" and not upc:
    st.info("Enter a UPC to run the report.")
    st.stop()

if st.button("Run report", type="primary"):
    with st.spinner(f"Fetching sales for {len(selected_stores)} store(s)..."):
        if search_mode == "Category":
            results = run_category_report(
                selected_stores, categories_by_store, selected_category, start_date, end_date
            )
        elif search_mode == "Keyword":
            results = run_keyword_report(selected_stores, keyword, start_date, end_date)
        else:
            results = run_upc_report(selected_stores, upc, start_date, end_date)

    rows = []
    missing_stores = []
    for store_key in selected_stores:
        store_name = connected_stores[store_key].get("name") or store_key
        result = results.get(store_key)
        if result is None:
            missing_stores.append(store_name)
            rows.append({"Store": store_name, "Total Sales": 0.0, "Units Sold": 0.0})
        else:
            rows.append({
                "Store": store_name,
                "Total Sales": result["total"],
                "Units Sold": result["quantity"],
            })

    # A "no result" for Category mode can mean the category name doesn't
    # exist at that store (worth flagging). For Keyword mode, $0 legitimately
    # just means nothing matching that keyword sold there - no warning needed.
    if search_mode == "Category" and missing_stores:
        st.warning(
            f"No category named \"{selected_category}\" found at: "
            f"{', '.join(missing_stores)}. Shown as $0 below - this likely "
            f"means that store uses a slightly different category name."
        )

    df = pd.DataFrame(rows)
    total_row = pd.DataFrame([{
        "Store": "TOTAL",
        "Total Sales": df["Total Sales"].sum(),
        "Units Sold": df["Units Sold"].sum(),
    }])
    df = pd.concat([df, total_row], ignore_index=True)

    st.dataframe(
        df,
        column_config={
            "Total Sales": st.column_config.NumberColumn(format="$%.2f"),
            "Units Sold": st.column_config.NumberColumn(format="%.0f"),
        },
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Item breakdown by store")
    for store_key in selected_stores:
        store_name = connected_stores[store_key].get("name") or store_key
        result = results.get(store_key)
        items = result.get("items") if result else None

        with st.expander(f"{store_name}"):
            if not items:
                st.caption("No matching items sold in this period.")
                continue

            item_df = pd.DataFrame([
                {
                    "Item": row["description"],
                    "Units Sold": row["quantity"],
                    "Total Sales": row["total"],
                }
                for row in items
            ])
            st.dataframe(
                item_df,
                column_config={
                    "Total Sales": st.column_config.NumberColumn(format="$%.2f"),
                    "Units Sold": st.column_config.NumberColumn(format="%.0f"),
                },
                hide_index=True,
                use_container_width=True,
            )

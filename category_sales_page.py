"""
Category Sales page (category_sales_page.py).

Lets the user search sales three ways:
  - Category: pick a category (e.g. "Rolling Trays") from a dropdown
  - Keyword: type a keyword (e.g. "mug") and it matches any item whose
    description contains that word
  - UPC: type an exact UPC and it matches the one item with that code

Either way, pick a date range and which stores to include, and it shows
how much each selected store sold for that search over that period, with
each store's row expandable to show exactly which items (alphabetically)
contributed to that total.

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

# Tighten default Streamlit spacing between the per-store blocks and their
# expanders below - best-effort CSS against Streamlit's current internal
# class names (pinned streamlit==1.56.0 in requirements.txt); if a future
# Streamlit upgrade changes these class names this simply becomes a no-op,
# it won't break the page.
st.markdown(
    """
    <style>
    div[data-testid="stHorizontalBlock"] { margin-bottom: 0rem; gap: 0.5rem; }
    div[data-testid="stExpander"] { margin-top: 0rem; margin-bottom: 0.5rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

SORT_DEFAULT_DIR = {"Store": "asc", "Total Sales": "desc", "Units Sold": "desc"}


def _sort_arrow(field, sort_field, sort_dir):
    if field != sort_field:
        return ""
    return " \u25b2" if sort_dir == "asc" else " \u25bc"


def _toggle_sort(field):
    """Click handler for a header button: flips direction if it's already
    the active sort field, otherwise switches to that field at its default
    direction."""
    current_field = st.session_state.get("cat_sales_sort_field", "Store")
    current_dir = st.session_state.get("cat_sales_sort_dir", "asc")
    if field == current_field:
        st.session_state["cat_sales_sort_dir"] = "desc" if current_dir == "asc" else "asc"
    else:
        st.session_state["cat_sales_sort_field"] = field
        st.session_state["cat_sales_sort_dir"] = SORT_DEFAULT_DIR[field]


def render_report(report):
    """Renders a previously-fetched report from session state. Kept
    separate from the fetch so clicking a sort header (which triggers its
    own script rerun) re-sorts and re-renders the cached results instead of
    re-fetching from Lightspeed."""
    results = report["results"]
    search_mode = report["search_mode"]
    selected_category = report["selected_category"]
    store_names = report["store_names"]
    selected_stores = report["selected_stores"]

    missing_stores = [
        store_names[store_key]
        for store_key in selected_stores
        if results.get(store_key) is None
    ]

    # A "no result" for Category mode can mean the category name doesn't
    # exist at that store (worth flagging). For Keyword/UPC mode, $0
    # legitimately just means nothing matching sold there - no warning needed.
    if search_mode == "Category" and missing_stores:
        st.warning(
            f"No category named \"{selected_category}\" found at: "
            f"{', '.join(missing_stores)}. Shown as $0 below - this likely "
            f"means that store uses a slightly different category name."
        )

    rows = []
    for store_key in selected_stores:
        result = results.get(store_key)
        rows.append({
            "store_name": store_names[store_key],
            "total": result["total"] if result else 0.0,
            "quantity": result["quantity"] if result else 0.0,
            "items": result.get("items") if result else None,
        })

    sort_field = st.session_state.get("cat_sales_sort_field", "Store")
    sort_dir = st.session_state.get("cat_sales_sort_dir", "asc")
    sort_key = {
        "Store": lambda row: row["store_name"].casefold(),
        "Total Sales": lambda row: row["total"],
        "Units Sold": lambda row: row["quantity"],
    }[sort_field]
    rows.sort(key=sort_key, reverse=(sort_dir == "desc"))

    col_widths = [3, 2, 2]

    header_cols = st.columns(col_widths)
    if header_cols[0].button(f"Store{_sort_arrow('Store', sort_field, sort_dir)}", key="cat_sales_sort_store", use_container_width=True):
        _toggle_sort("Store")
        st.rerun()
    if header_cols[1].button(f"Total Sales{_sort_arrow('Total Sales', sort_field, sort_dir)}", key="cat_sales_sort_total", use_container_width=True):
        _toggle_sort("Total Sales")
        st.rerun()
    if header_cols[2].button(f"Units Sold{_sort_arrow('Units Sold', sort_field, sort_dir)}", key="cat_sales_sort_units", use_container_width=True):
        _toggle_sort("Units Sold")
        st.rerun()

    total_sales_sum = 0.0
    units_sold_sum = 0.0

    for row in rows:
        total_sales_sum += row["total"]
        units_sold_sum += row["quantity"]

        row_cols = st.columns(col_widths)
        row_cols[0].write(row["store_name"])
        row_cols[1].write(f"${row['total']:,.2f}")
        row_cols[2].write(f"{row['quantity']:,.0f}")

        with st.expander("Item breakdown"):
            items = row["items"]
            if not items:
                st.caption("No matching items sold in this period.")
            else:
                item_df = pd.DataFrame([
                    {
                        "Item": item_row["description"],
                        "Units Sold": item_row["quantity"],
                        "Total Sales": item_row["total"],
                    }
                    for item_row in items
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

    total_cols = st.columns(col_widths)
    total_cols[0].markdown("**TOTAL**")
    total_cols[1].markdown(f"**${total_sales_sum:,.2f}**")
    total_cols[2].markdown(f"**{units_sold_sum:,.0f}**")


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

all_store_keys = sorted(
    connected_stores.keys(),
    key=lambda store_key: (connected_stores[store_key].get("name") or store_key).casefold(),
)

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
    # multiselect returns picks in click order, not option order - re-sort
    # alphabetically so store blocks below are always in the same order.
    selected_stores = sorted(
        selected_stores,
        key=lambda store_key: (connected_stores[store_key].get("name") or store_key).casefold(),
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

    # Cached in session state (rather than rendered immediately) so that
    # clicking a sort header afterward - which triggers its own script
    # rerun - can re-sort and re-render these same results instead of
    # re-fetching from Lightspeed every time.
    st.session_state["cat_sales_report"] = {
        "search_mode": search_mode,
        "selected_category": selected_category,
        "results": results,
        "store_names": {
            store_key: connected_stores[store_key].get("name") or store_key
            for store_key in selected_stores
        },
        "selected_stores": selected_stores,
    }

report = st.session_state.get("cat_sales_report")
if report:
    render_report(report)

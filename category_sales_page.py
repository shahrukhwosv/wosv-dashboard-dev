"""
Category Sales page (category_sales_page.py).

Lets the user search sales three ways:
  - Category: pick a category (e.g. "Rolling Trays") from a dropdown
  - Keyword: type a keyword (e.g. "mug") and it matches any item with that
    exact word in its description (whole-word match, not substring - "raw"
    matches "RAW King Size Slims" but not "Strawberry Vape Juice").
    Comma-separate multiple words to match any of them (e.g. "mama, pipe").
    An optional "Exclude keyword(s)" field (also comma-separatable) drops
    any item that also contains one of those words (e.g. keyword "pipe" +
    exclude "water" keeps "Glass Hand Pipe" but drops "18in Water Pipe").
    A quick preset button is available for the common "Mama (excl. Pacha)"
    search.
  - UPC: type an exact UPC and it matches the one item with that code

Either way, pick a date range (or use the This Month/Last Month/This Year
shortcuts) and which stores to include, and it shows how much each
selected store sold for that search over that period, with each store's
row expandable to show exactly which items (alphabetically) contributed
to that total. A "See Trend" toggle shows a combined (all selected
stores summed together, not per-store) Units Sold line chart - bucketed
by day for ranges of 30 days or fewer, or by 7-day chunks (labeled by
each chunk's start date) for longer ranges.

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
from store_access import get_page_store_keys

st.title("Category Sales")

# Styling for the results section (stat cards, table header, pill buttons,
# etc.) to visually match the reference "Rank Report" mockup the user
# supplied - best-effort CSS against Streamlit's current internal class
# names (pinned streamlit==1.56.0 in requirements.txt); if a future
# Streamlit upgrade changes these class names this simply becomes a no-op,
# it won't break the page. Search/filter controls above the results
# (mode, category, stores, dates) are left as native Streamlit widgets -
# only the results section below "Run report" is restyled.
st.markdown(
    """
    <style>
    div[data-testid="stHorizontalBlock"] { margin-bottom: 0rem; gap: 0.5rem; }
    hr { margin: 0.2rem 0 !important; }

    /* Stat cards (Total Sales / Total Units Sold) - capped to the same
       width as the table below instead of stretching across the page. */
    .cat-stat-row {
        display: flex;
        border: 1px solid rgba(128, 128, 128, 0.25);
        border-radius: 12px;
        overflow: hidden;
        margin-bottom: 0.75rem;
        max-width: 640px;
    }
    .cat-stat-card {
        flex: 1;
        display: flex;
        align-items: center;
        gap: 0.75rem;
        padding: 1rem 1.25rem;
    }
    .cat-stat-card + .cat-stat-card {
        border-left: 1px solid rgba(128, 128, 128, 0.25);
    }
    .cat-icon-circle {
        width: 42px;
        height: 42px;
        min-width: 42px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 1.1rem;
    }
    .cat-icon-red { background: rgba(239, 68, 68, 0.12); }
    .cat-icon-blue { background: rgba(59, 130, 246, 0.12); }
    .cat-stat-label {
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: rgba(100, 100, 100, 0.9);
    }
    .cat-stat-value { font-size: 1.4rem; font-weight: 700; line-height: 1.3; }
    .cat-stat-sub { font-size: 0.78rem; color: rgba(120, 120, 120, 0.9); }

    /* Table header capsule */
    .st-key-cat_sales_header {
        background: rgba(128, 128, 128, 0.06);
        border-radius: 10px 10px 0 0;
        padding: 0.5rem 0.75rem 0.5rem 0.75rem;
    }
    .st-key-cat_sales_header button {
        background: transparent !important;
        border: none !important;
        font-weight: 600;
        padding: 0.1rem 0.2rem;
        color: inherit;
    }
    .st-key-cat_sales_header button:hover { text-decoration: underline; }

    .cat-cell-store { display: flex; align-items: center; gap: 0.5rem; padding-top: 0.3rem; }
    .cat-store-icon {
        width: 30px;
        height: 30px;
        min-width: 30px;
        border-radius: 50%;
        background: rgba(128, 128, 128, 0.12);
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 0.9rem;
    }
    .cat-cell-center { text-align: center; padding-top: 0.3rem; }
    .cat-value-primary { font-weight: 600; font-size: 0.95rem; }
    .cat-value-green { font-weight: 700; font-size: 0.95rem; color: #16a34a; }

    .st-key-cat_sales_table .stButton button {
        border-radius: 999px;
        padding: 0.15rem 0.7rem;
        font-size: 0.8rem;
        font-weight: 500;
    }

    /* Cap the results table's width instead of letting it stretch across
       the full page - it only has a few short columns, so full width
       just spreads everything out with a lot of empty gap. */
    .st-key-cat_sales_table { max-width: 640px; }
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


def _right_align(text):
    st.markdown(f"<div style='text-align: right'>{text}</div>", unsafe_allow_html=True)


def render_report(report):
    """Renders a previously-fetched report from session state. Kept
    separate from the fetch so clicking a sort header or a "Top Items"
    pill (each triggers its own script rerun) re-renders the cached
    results instead of re-fetching from Lightspeed."""
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
        items = result.get("items") if result else None
        rows.append({
            "store_key": store_key,
            "store_name": store_names[store_key],
            "total": result["total"] if result else 0.0,
            "quantity": result["quantity"] if result else 0.0,
            "items": items,
            "item_count": len(items) if items else 0,
        })

    sort_field = st.session_state.get("cat_sales_sort_field", "Store")
    sort_dir = st.session_state.get("cat_sales_sort_dir", "asc")
    sort_key = {
        "Store": lambda row: row["store_name"].casefold(),
        "Total Sales": lambda row: row["total"],
        "Units Sold": lambda row: row["quantity"],
    }[sort_field]
    rows.sort(key=sort_key, reverse=(sort_dir == "desc"))

    total_sales_sum = sum(row["total"] for row in rows)
    units_sold_sum = sum(row["quantity"] for row in rows)

    # Stat cards
    st.markdown(
        f"""
        <div class="cat-stat-row">
            <div class="cat-stat-card">
                <div class="cat-icon-circle cat-icon-red">\U0001F6CD\uFE0F</div>
                <div>
                    <div class="cat-stat-label">Total Sales</div>
                    <div class="cat-stat-value">${total_sales_sum:,.2f}</div>
                    <div class="cat-stat-sub">Across {len(rows)} store{'s' if len(rows) != 1 else ''}</div>
                </div>
            </div>
            <div class="cat-stat-card">
                <div class="cat-icon-circle cat-icon-blue">\U0001F4E6</div>
                <div>
                    <div class="cat-stat-label">Total Units Sold</div>
                    <div class="cat-stat-value">{units_sold_sum:,.0f}</div>
                    <div class="cat-stat-sub">Across {len(rows)} store{'s' if len(rows) != 1 else ''}</div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    _render_trend_section(report, results, selected_stores)

    col_widths = [3, 2, 2, 2]
    expanded = st.session_state.setdefault("cat_sales_expanded", set())

    with st.container(key="cat_sales_table"):
        with st.container(key="cat_sales_header"):
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
            header_cols[3].markdown("<div class='cat-stat-label' style='padding-top: 0.4rem;'>Top Items</div>", unsafe_allow_html=True)

        for row in rows:
            store_key = row["store_key"]
            row_cols = st.columns(col_widths)
            row_cols[0].markdown(
                f"<div class='cat-cell-store'><div class='cat-store-icon'>\U0001F3EA</div>{row['store_name']}</div>",
                unsafe_allow_html=True,
            )
            row_cols[1].markdown(
                f"<div class='cat-cell-center cat-value-primary'>${row['total']:,.2f}</div>",
                unsafe_allow_html=True,
            )
            row_cols[2].markdown(
                f"<div class='cat-cell-center cat-value-green'>{row['quantity']:,.0f}</div>",
                unsafe_allow_html=True,
            )
            with row_cols[3]:
                is_open = store_key in expanded
                chevron = "\u25be" if is_open else "\u203a"
                label = f"{row['item_count']} items {chevron}"
                if st.button(label, key=f"cat_sales_pill_{store_key}", use_container_width=True):
                    if is_open:
                        expanded.discard(store_key)
                    else:
                        expanded.add(store_key)
                    st.rerun()

            if store_key in expanded:
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

            st.divider()


def _build_trend_dataframe(combined_by_day, start_date, end_date):
    """Builds a Units Sold trend DataFrame, indexed by whole-day category
    labels (never fractional/sub-day ticks) rather than a continuous date
    axis. 30 days or fewer buckets by day; longer ranges bucket into 7-day
    chunks starting from start_date, labeled by each chunk's start date."""
    total_days = (end_date - start_date).days + 1
    all_days = [start_date + timedelta(days=i) for i in range(total_days)]

    labels = []
    values = []
    if total_days <= 30:
        for day in all_days:
            labels.append(day.strftime("%b %d"))
            values.append(combined_by_day.get(day.isoformat(), {}).get("quantity", 0.0))
    else:
        for i in range(0, total_days, 7):
            chunk = all_days[i:i + 7]
            labels.append(chunk[0].strftime("%b %d"))
            values.append(sum(
                combined_by_day.get(day.isoformat(), {}).get("quantity", 0.0)
                for day in chunk
            ))

    return pd.DataFrame({"Units Sold": values}, index=pd.Index(labels, name="Date"))


def _render_trend_section(report, results, selected_stores):
    """Combined (all-stores) trend line for the current search, toggled via
    a 'See Trend' button. Independent of the table's sort/expand state -
    it always covers every selected store and the full selected date
    range, regardless of table sorting. Buckets by day for ranges of 30
    days or fewer, otherwise by 7-day chunks from the start date - see
    _build_trend_dataframe."""
    show_trend = st.session_state.get("cat_sales_show_trend", False)
    if st.button("See Trend" if not show_trend else "Hide Trend", key="cat_sales_trend_toggle"):
        st.session_state["cat_sales_show_trend"] = not show_trend
        st.rerun()

    if not show_trend:
        return

    combined_by_day = {}
    for store_key in selected_stores:
        result = results.get(store_key)
        if not result:
            continue
        for day_key, day_totals in result.get("by_day", {}).items():
            bucket = combined_by_day.setdefault(day_key, {"total": 0.0, "quantity": 0.0})
            bucket["total"] += day_totals["total"]
            bucket["quantity"] += day_totals["quantity"]

    trend_df = _build_trend_dataframe(combined_by_day, report["start_date"], report["end_date"])
    st.line_chart(trend_df, use_container_width=True)


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


def run_keyword_report(store_keys, keyword, exclude_keyword, start_date, end_date):
    """Fetches keyword sales for each store in parallel."""
    config = ls.load_config()
    results = {}

    def _fetch_one(store_key):
        return store_key, ls.fetch_keyword_sales(
            config, store_key, keyword, start_date, end_date, exclude_keyword=exclude_keyword
        )

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

visible_keys = get_page_store_keys(config)  # no list_name - unrestricted, same as before
connected_stores = {
    key: val for key, val in stores.items()
    if key in visible_keys and not val.get("pace_only")
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
exclude_keyword = None
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
    KEYWORD_PRESETS = {
        "Mama (excl. Pacha)": ("mama", "pacha"),
    }
    preset_button_cols = st.columns(len(KEYWORD_PRESETS) + 3)  # extra room so buttons don't stretch full width
    for col, (preset_label, (preset_keyword, preset_exclude)) in zip(preset_button_cols, KEYWORD_PRESETS.items()):
        if col.button(preset_label, key=f"cat_sales_preset_{preset_label}"):
            st.session_state["cat_sales_keyword"] = preset_keyword
            st.session_state["cat_sales_exclude"] = preset_exclude

    keyword_cols = st.columns(2)
    with keyword_cols[0]:
        keyword = st.text_input(
            "Keyword",
            value=st.session_state.get("cat_sales_keyword", ""),
            placeholder="e.g. mama, pipe",
            help="Comma-separate multiple words to match any of them. Whole-word match only (plus plural/possessive) - \"raw\" won't match \"strawberry\"",
            key="cat_sales_keyword",
        ).strip()
    with keyword_cols[1]:
        exclude_keyword = st.text_input(
            "Exclude keyword(s) (optional)",
            value=st.session_state.get("cat_sales_exclude", ""),
            placeholder="e.g. water, pacha",
            help="Comma-separate multiple words - drops any item containing any of them, e.g. exclude \"water\" to keep \"pipe\" from matching \"water pipe\"",
            key="cat_sales_exclude",
        ).strip() or None
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

preset_cols = st.columns(3)
if preset_cols[0].button("This Month", use_container_width=True):
    today = date.today()
    st.session_state["cat_sales_start"] = today.replace(day=1)
    st.session_state["cat_sales_end"] = today
if preset_cols[1].button("Last Month", use_container_width=True):
    first_of_this_month = date.today().replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    st.session_state["cat_sales_start"] = last_of_prev_month.replace(day=1)
    st.session_state["cat_sales_end"] = last_of_prev_month
if preset_cols[2].button("This Year", use_container_width=True):
    today = date.today()
    st.session_state["cat_sales_start"] = today.replace(month=1, day=1)
    st.session_state["cat_sales_end"] = today

col1, col2 = st.columns(2)
with col1:
    start_date = st.date_input(
        "Start date",
        value=st.session_state.get("cat_sales_start", date.today() - timedelta(days=30)),
        key="cat_sales_start",
    )
with col2:
    end_date = st.date_input(
        "End date",
        value=st.session_state.get("cat_sales_end", date.today()),
        key="cat_sales_end",
    )

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
            results = run_keyword_report(selected_stores, keyword, exclude_keyword, start_date, end_date)
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
        "start_date": start_date,
        "end_date": end_date,
    }

report = st.session_state.get("cat_sales_report")
if report:
    render_report(report)

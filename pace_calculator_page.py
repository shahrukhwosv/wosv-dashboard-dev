"""
Pace Calculator page.

Shows each store's previous day's sales, projected monthly total, and
projected annual total, using a flat run-rate calculation against the
cached daily sales log (see sales_pace.py).

Add this file to your app's navigation the same way Commissions/
Transactions/Touch Tell are registered in app.py, and rename it to match
your existing pages' naming convention if needed.

NOTE ON store display names: this assumes each entry in stores_config.json
may have a "display_name" field to show instead of the raw store key. If it
doesn't, this just falls back to showing the store key itself - adjust
store_names below if you keep display names somewhere else (e.g. hardcoded
in stores_config.json under a different field, or in a separate mapping).

PASSWORD: set PACE_CALCULATOR_PASSWORD as an environment variable (locally
and on Railway, same pattern as your other env vars). There's also a
hardcoded fallback below for local use, same pattern used for
PACE_LOG_SHEET_ID in sales_pace.py - replace the placeholder with your own
password.
"""
import os
import json

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from lightspeed_client import load_config
from sales_pace import compute_pace, month_actual_total, read_daily_log, update_daily_log
from pace_pdf_report import MONTH_NAMES, build_monthly_pdf

PAGE_PASSWORD = os.getenv("PACE_CALCULATOR_PASSWORD", "PASTE_A_PASSWORD_HERE")

if "pace_calculator_unlocked" not in st.session_state:
    st.session_state.pace_calculator_unlocked = False

if not st.session_state.pace_calculator_unlocked:
    st.title("Sales Pace Calculator")
    with st.form("pace_password_form"):
        entered_password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Unlock")
    if submitted:
        if entered_password == PAGE_PASSWORD:
            st.session_state.pace_calculator_unlocked = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()

st.title("Sales Pace Calculator")

config = load_config()
store_keys = list(config["stores"].keys())
store_names = {
    key: config["stores"][key].get("name", key) for key in store_keys
}

col1, col2 = st.columns(2)
with col1:
    if st.button("Reload from sheet"):
        st.cache_data.clear()
        st.success("Reloaded.")
with col2:
    if st.button("Fetch missing days from Lightspeed"):
        with st.spinner("Fetching any missing days from Lightspeed - this can take a while for stores with little/no history yet..."):
            added = update_daily_log(config, store_keys)
        st.success(f"Log updated - added {added} new day(s) of data.")
        st.cache_data.clear()


@st.cache_data(ttl=3600)
def _load_log():
    return read_daily_log()


df = _load_log()

if df.empty:
    st.info(
        "No sales data logged yet. Run backfill_pace_log.py once from your "
        "terminal, then click 'Reload from sheet' above."
    )
else:
    rows = []
    for store_key in store_keys:
        pace = compute_pace(df, store_key)
        as_of = pace["as_of"]
        rows.append({
            "store": store_names[store_key],
            # as_of_sort is a real sortable value (ISO date or empty string,
            # which sorts first); as_of_display is what's actually shown -
            # sorting on "7/9" vs "7/10" as plain text would misorder them.
            "as_of_sort": as_of.isoformat() if as_of else "",
            "as_of_display": f"{as_of.month}/{as_of.day}" if as_of else "-",
            "latest_sales": pace["yesterday_total"],
            "projected_monthly": pace["projected_monthly"],
            "projected_annual": pace["projected_annual"],
        })

    rows.sort(key=lambda row: row["store"])

    table_height = 40 + len(rows) * 36

    components.html(
        f"""
        <style>
        :root {{
            --pace-bg: #ffffff;
            --pace-text: #262730;
            --pace-border: #cccccc;
        }}
        body {{
            font-family: "Source Sans Pro", Arial, sans-serif;
            margin: 0;
            color: var(--pace-text);
            background: var(--pace-bg);
        }}
        .pace-table-wrap {{
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .pace-table {{
            width: 100%;
            min-width: 640px;
            border-collapse: collapse;
        }}
        .pace-table th {{
            font-weight: 700;
            text-align: left;
            padding: 8px 12px;
            border-bottom: 2px solid var(--pace-border);
            cursor: pointer;
            user-select: none;
            white-space: nowrap;
            background: var(--pace-bg);
        }}
        .pace-table th:hover {{
            color: #ff4b4b;
        }}
        .pace-table th .arrow {{
            font-size: 11px;
            opacity: 0.6;
            margin-left: 4px;
        }}
        .pace-table td {{
            padding: 8px 12px;
            border-bottom: 1px solid var(--pace-border);
            white-space: nowrap;
        }}
        .pace-table td.store-cell, .pace-table th[data-key="store"] {{
            font-weight: 700;
            position: sticky;
            left: 0;
            background: var(--pace-bg);
            z-index: 1;
        }}
        .pace-table th[data-key="store"] {{
            z-index: 2;
        }}
        </style>
        <div class="pace-table-wrap">
        <table class="pace-table" id="pace-table">
            <thead>
                <tr>
                    <th data-key="store" data-type="string">Store<span class="arrow"></span></th>
                    <th data-key="as_of_sort" data-type="string">As Of<span class="arrow"></span></th>
                    <th data-key="latest_sales" data-type="number">Latest Day's Sales<span class="arrow"></span></th>
                    <th data-key="projected_monthly" data-type="number">Projected Monthly Total<span class="arrow"></span></th>
                    <th data-key="projected_annual" data-type="number">Projected Annual Total<span class="arrow"></span></th>
                </tr>
            </thead>
            <tbody id="pace-table-body"></tbody>
        </table>
        </div>
        <script>
            // Detect whether Streamlit's ACTUAL current theme (whatever the
            // user has it set to) is light or dark, then apply one of two
            // known-good, pre-tested color pairs - rather than copying raw
            // background/text values independently, which risks pairing a
            // transparent background from one element with a light text
            // color from another and ending up unreadable either way.
            function parseRgb(str) {{
                const m = str.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
                if (!m) return null;
                return {{
                    r: parseInt(m[1]), g: parseInt(m[2]), b: parseInt(m[3]),
                    a: m[4] !== undefined ? parseFloat(m[4]) : 1
                }};
            }}

            function applyTheme(isDark) {{
                if (isDark) {{
                    document.documentElement.style.setProperty("--pace-bg", "#0e1117");
                    document.documentElement.style.setProperty("--pace-text", "#fafafa");
                    document.documentElement.style.setProperty("--pace-border", "#444444");
                }} else {{
                    document.documentElement.style.setProperty("--pace-bg", "#ffffff");
                    document.documentElement.style.setProperty("--pace-text", "#262730");
                    document.documentElement.style.setProperty("--pace-border", "#cccccc");
                }}
            }}

            function syncTheme() {{
                try {{
                    const parentDoc = window.parent.document;
                    const candidates = [
                        '[data-testid="stAppViewContainer"]',
                        '[data-testid="stApp"]',
                        '.stApp',
                        'body'
                    ];
                    for (const selector of candidates) {{
                        const el = parentDoc.querySelector(selector);
                        if (!el) continue;
                        const rgb = parseRgb(window.parent.getComputedStyle(el).backgroundColor);
                        if (!rgb || rgb.a === 0) continue;
                        // Standard relative luminance formula.
                        const luminance = (0.299 * rgb.r + 0.587 * rgb.g + 0.114 * rgb.b) / 255;
                        applyTheme(luminance < 0.5);
                        return;
                    }}
                }} catch (e) {{
                    // Cross-origin or other failure - fall back to light,
                    // the CSS defaults already set above.
                }}
            }}
            syncTheme();
            // Streamlit's light/dark toggle can change the page's colors
            // client-side without a full reload, so keep re-checking rather
            // than detecting the theme only once when this table first
            // loads - otherwise switching themes after load leaves the
            // table stuck on whatever it detected the first time.
            setInterval(syncTheme, 1000);

            let rows = {json.dumps(rows)};
            let sortKey = "store";
            let sortAsc = true;

            function money(value) {{
                return "$" + value.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
            }}

            function render() {{
                const tbody = document.getElementById("pace-table-body");
                tbody.innerHTML = rows.map(r => (
                    "<tr>" +
                    "<td class='store-cell'>" + r.store + "</td>" +
                    "<td>" + r.as_of_display + "</td>" +
                    "<td>" + money(r.latest_sales) + "</td>" +
                    "<td>" + money(r.projected_monthly) + "</td>" +
                    "<td>" + money(r.projected_annual) + "</td>" +
                    "</tr>"
                )).join("");

                document.querySelectorAll("#pace-table th").forEach(th => {{
                    const arrow = th.querySelector(".arrow");
                    if (th.dataset.key === sortKey) {{
                        arrow.textContent = sortAsc ? "▲" : "▼";
                    }} else {{
                        arrow.textContent = "";
                    }}
                }});
            }}

            function sortRows(key, type) {{
                if (sortKey === key) {{
                    sortAsc = !sortAsc;
                }} else {{
                    sortKey = key;
                    sortAsc = true;
                }}
                rows.sort((a, b) => {{
                    let av = a[key], bv = b[key];
                    let cmp = type === "number" ? (av - bv) : String(av).localeCompare(String(bv));
                    return sortAsc ? cmp : -cmp;
                }});
                render();
            }}

            document.querySelectorAll("#pace-table th").forEach(th => {{
                th.addEventListener("click", () => sortRows(th.dataset.key, th.dataset.type));
            }});

            render();
        </script>
        """,
        height=table_height,
    )

    # Regional groupings, matched by store name (not store_key, since key
    # numbering doesn't reflect any north/south grouping). Note: the config
    # names this store "Greenville" (not "Lower Greenville") - matched
    # accordingly below.
    NORTH_STORES = {"Aubrey", "Rowlett", "Princeton", "Frisco", "Liquor Depot"}
    SOUTH_STORES = {"Oak Lawn", "Greenville", "West Greenville", "Lovers", "Hillcrest"}

    pace_by_name = {row["store"]: row["projected_monthly"] for row in rows}

    north_total = sum(pace_by_name.get(name, 0.0) for name in NORTH_STORES)
    south_total = sum(pace_by_name.get(name, 0.0) for name in SOUTH_STORES)

    north_missing = sorted(NORTH_STORES - pace_by_name.keys())
    south_missing = sorted(SOUTH_STORES - pace_by_name.keys())

    def _flatten_html(html):
        """Strips leading whitespace from every line. Markdown treats any
        line indented 4+ spaces as a literal code block, so nested/spliced
        HTML fragments (which can end up with inconsistent indentation)
        need every line flattened to column 0, not just a shared prefix
        removed - textwrap.dedent() alone isn't reliable here since it only
        strips a COMMON prefix, which breaks once differently-indented
        fragments get merged together."""
        return "\n".join(line.lstrip() for line in html.strip().splitlines())

    def _region_block(label, total, missing):
        missing_html = (
            f"<div class='region-missing'>Not found: {', '.join(missing)}</div>"
            if missing else ""
        )
        return _flatten_html(f"""
            <div class="region-block">
                <div class="region-label">{label}</div>
                <div class="region-value">${total:,.2f}</div>
                {missing_html}
            </div>
        """)

    region_html = _flatten_html(f"""
        <style>
        .region-wrap {{
            margin-top: -10px;
        }}
        .region-divider {{
            margin: 2px 0 4px 0;
            border: none;
            border-top: 1px solid #333;
        }}
        .region-row {{
            display: flex;
            gap: 6px;
        }}
        .region-block {{
            flex: 0 0 auto;
        }}
        .region-label {{
            font-size: 0.875rem;
            opacity: 0.7;
        }}
        .region-value {{
            font-size: 1.125rem;
            font-weight: 600;
        }}
        .region-missing {{
            font-size: 0.75rem;
            opacity: 0.6;
        }}
        </style>
        <div class="region-wrap">
            <hr class="region-divider">
            <div class="region-row">
                {_region_block("North Stores Pace", north_total, north_missing)}
                {_region_block("South Stores Pace", south_total, south_missing)}
            </div>
        </div>
    """)

    st.markdown(region_html, unsafe_allow_html=True)

    st.divider()
    st.subheader("Monthly PDF Report")

    col1, col2, col3 = st.columns([1, 1, 1])
    current_year = df["date"].max().year if not df.empty else None
    with col1:
        selected_month_name = st.selectbox("Month", MONTH_NAMES, index=6)
        selected_month = MONTH_NAMES.index(selected_month_name) + 1
    with col2:
        year_options = list(range(2024, 2031))
        default_year_index = year_options.index(current_year) if current_year in year_options else len(year_options) - 1
        selected_year = st.selectbox("Year", year_options, index=default_year_index)
    with col3:
        st.write("")  # vertical spacer to align the button with the dropdowns
        st.write("")
        get_pdf_clicked = st.button("Get PDF")

    if get_pdf_clicked:
        store_rows = []
        for store_key in store_keys:
            total = month_actual_total(df, store_key, selected_year, selected_month)
            store_rows.append((store_names[store_key], total))
        store_rows.sort(key=lambda r: r[0])

        monthly_by_name = {name: total for name, total in store_rows}
        north_month_total = sum(monthly_by_name.get(name, 0.0) for name in NORTH_STORES)
        south_month_total = sum(monthly_by_name.get(name, 0.0) for name in SOUTH_STORES)

        pdf_buffer = build_monthly_pdf(
            store_rows, north_month_total, south_month_total, selected_month, selected_year
        )

        st.download_button(
            label="Download PDF",
            data=pdf_buffer,
            file_name=f"sales_report_{selected_month_name}_{selected_year}.pdf",
            mime="application/pdf",
        )

"""
Main Dashboard page - company-wide sales snapshot for yesterday, plus a
6-month trend.

Total Sales, Highest/Lowest Store, Top Performing Stores, and the Monthly
Trend chart all read directly from the pace log Google Sheet (via
sales_pace.read_daily_log()) - the SAME sheet the Pace Calculator page
uses, so this page never makes a live Lightspeed API call itself.

Both that sheet and Mama's Sold (see below) are kept current by
nightly_refresh.py, meant to run once a night via a Railway Cron Job -
see that file for what it does and how to set it up. This page just
reads whatever nightly_refresh.py last saved; it does NOT fetch anything
live on page load. If nobody's run that job recently, numbers here can be
stale - there's a caption below showing the most recent date actually
found in the sheet so that's visible at a glance.

Mama's Sold specifically comes from dashboard_data.load_mama_snapshot() -
nightly_refresh.py computes it once a night (the pace log sheet only
tracks each store's total dollars per day, not a category/keyword
breakdown). It's also the one metric that intentionally covers all
connected stores (unrestricted - the same scope Category Sales uses), not
the Pace Calculator Stores list the rest of the page uses, and the ONLY
KPI card with no sparkline - the nightly job only keeps the latest day's
snapshot, not a running history, so there's no real trend to draw.

SPARKLINES on the other four cards ARE real, not fabricated - the pace
log already has months of daily history per store, this just hadn't been
used yet:
  - Total Sales: last 7 days, company-wide daily total
  - Highest/Lowest Store: last 7 days, that specific store's own daily
    total (which store is "highest" is evaluated once for snapshot_date
    and then that store's own trend is drawn - it does not re-pick a
    different store for earlier days)
  - MTD Pace: cumulative day-by-day total so far this month (naturally
    short early in the month - omitted entirely if fewer than 2 days of
    data exist yet, rather than drawing a single dot)

MTD Pace reuses sales_pace.compute_pace() (same projection math as the
Pace Calculator page) per store, summed into one company-wide projected
total for the current month, compared against last month's actual total
(sales_pace.month_actual_total(), also already existing).

Total Sales, Highest Store, and Lowest Store show the actual dollar
figure rather than a day-over-day comparison. Top Performing Stores
still shows a real day-over-day % change per store (same underlying
day_before_by_store data), since that panel is specifically about
ranking/movement, not a single headline number.

Everything except Mama's Sold uses the Pace Calculator Stores list
(same scope, and same require_connected=False behavior, as the Pace
Calculator page) - editable from Manage Users. Mama's Sold stays
unrestricted regardless.

NOT built (would require inventing functionality/data that doesn't
exist): a global search bar, a real interactive date-range picker (every
metric here is single-day/MTD, not range-queryable without reworking the
underlying queries), and Units Sold/Transactions tabs on the chart (the
pace log only tracks dollar totals, not unit or transaction counts).
"""

from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

import lightspeed_client as ls
from dashboard_data import load_mama_snapshot
from sales_pace import compute_pace, month_actual_total, read_daily_log, store_local_today
from store_access import PACE_CALCULATOR_STORES_LIST, get_page_store_keys

CARD_STYLE = (
    "background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); "
    "border-radius: 12px;"
)
SPARK_DAYS = 7


def sparkline_svg(values, color, width=72, height=28):
    """Tiny inline SVG line - built by hand (not Altair) so it can live
    inside a custom HTML card; Streamlit widgets can't reliably nest
    inside a raw HTML div written via a separate st.markdown call.
    Returns "" if there isn't enough real data to draw a meaningful line."""
    values = [v for v in values if v is not None]
    if len(values) < 2 or min(values) == max(values):
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo
    n = len(values)
    points = []
    for i, v in enumerate(values):
        x = (i / (n - 1)) * (width - 4) + 2
        y = height - 2 - ((v - lo) / span) * (height - 4)
        points.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        f"</svg>"
    )


def daily_series(df, end_date, days, store_key=None):
    """Real daily totals for the given window - company-wide if
    store_key is None, one store's own totals otherwise. Missing dates
    are filled with 0 rather than skipped, so a gap in the log shows as
    a real dip instead of silently compressing the line."""
    start = end_date - timedelta(days=days - 1)
    window = df[(df["date"] >= start) & (df["date"] <= end_date)]
    if store_key:
        window = window[window["store"] == store_key]
    grouped = window.groupby("date")["total"].sum()
    all_dates = [start + timedelta(days=i) for i in range(days)]
    return [float(grouped.get(d, 0.0)) for d in all_dates]


def render_metric_card(label, value, delta_value=None, delta_period=None, delta_positive=None,
                        icon="•", icon_color="#6366F1", sparkline_html=""):
    """delta_positive: True (green), False (red), or None (neutral gray) -
    pass None when there's no real comparison to show.

    Built as ONE continuous string (no embedded newlines/indentation) on
    purpose - Streamlit's Markdown renderer treats a blank line followed
    by indented text as a code block, which is exactly what a
    pretty-printed multi-line f-string here triggered whenever a section
    (e.g. delta_html) came back empty. A single-line string can't
    produce that blank-line pattern no matter which parts are empty."""
    delta_html = ""
    if delta_value:
        color = "#22C55E" if delta_positive is True else "#EF4444" if delta_positive is False else "#9CA3AF"
        delta_html = f'<div style="font-size:0.82rem; color:{color}; font-weight:500;">{delta_value}</div>'
        if delta_period:
            delta_html += f'<div style="font-size:0.72rem; color:#6B7280; margin-top:1px;">{delta_period}</div>'
    return (
        f'<div style="{CARD_STYLE} padding: 1.1rem 1.2rem; height: 150px; display:flex; '
        f'flex-direction:column; justify-content:space-between;">'
        f'<div style="display:flex; align-items:center; gap:10px;">'
        f'<div style="width:40px; height:40px; border-radius:10px; background:{icon_color}26; '
        f'color:{icon_color}; display:flex; align-items:center; justify-content:center; '
        f'font-size:1.05rem; font-weight:700; flex-shrink:0;">{icon}</div>'
        f'<div style="font-size:0.8rem; color:#9CA3AF;">{label}</div>'
        f'</div>'
        f'<div style="display:flex; align-items:flex-end; justify-content:space-between; gap:8px;">'
        f'<div style="min-width:0;">'
        f'<div style="font-size:1.4rem; font-weight:700; line-height:1.25; overflow:hidden; '
        f'text-overflow:ellipsis; white-space:nowrap;">{value}</div>'
        f'{delta_html}'
        f'</div>'
        f'<div style="flex-shrink:0;">{sparkline_html}</div>'
        f'</div>'
        f'</div>'
    )


def pct_change(current, previous):
    if not previous:
        return None
    return (current - previous) / previous * 100


def pct_delta_text(change):
    if change is None:
        return None
    arrow = "\u2191" if change >= 0 else "\u2193"
    sign = "+" if change >= 0 else ""
    return f"{arrow} {sign}{change:.1f}%"


config = ls.load_config()
stores = config["stores"]

store_keys = list(get_page_store_keys(
    config,
    list_name=PACE_CALCULATOR_STORES_LIST,
    default_keys=list(stores.keys()),  # matches pace_calculator_page.py's own default
    require_connected=False,  # matches pace_calculator_page.py - shows every store key in the list, connected or not
))
store_names = {key: stores[key].get("name", key) for key in store_keys}

if not store_keys:
    st.title("Dashboard")
    st.info("No stores available. Check the Pace Calculator Stores list on the Manage Users page.")
    st.stop()

log_df = read_daily_log()
accessible_log_df = log_df[log_df["store"].isin(store_keys)]

if accessible_log_df.empty:
    st.title("Dashboard")
    st.warning(
        "No data found in the pace log sheet yet for your stores. Visit "
        "the Pace Calculator page and click 'Fetch missing days from "
        "Lightspeed' to populate it, or wait for the nightly refresh job."
    )
    st.stop()

most_recent_date = accessible_log_df["date"].max()
yesterday = store_local_today() - timedelta(days=1)
snapshot_date = min(yesterday, most_recent_date)

# --- Header row: title/subtitle on the left, a date badge on the right ---
header_col, badge_col = st.columns([3, 1])
with header_col:
    st.title("Dashboard")
    st.caption(f"Here's what's happening across your {len(store_keys)} store{'s' if len(store_keys) != 1 else ''}.")
with badge_col:
    st.markdown(
        f'<div style="{CARD_STYLE} padding: 0.6rem 1rem; text-align:center; margin-top: 1.6rem;">'
        f'<span style="font-size:0.85rem;">{snapshot_date.strftime("%b %d, %Y")}</span></div>',
        unsafe_allow_html=True,
    )

if most_recent_date < yesterday:
    st.warning(
        f"⚠️ The pace log sheet's most recent data is from "
        f"{most_recent_date.strftime('%B %d')} - nightly_refresh.py may not "
        f"have run recently. Numbers below may be out of date."
    )

# --- Yesterday's totals (or the most recent day actually in the sheet) ---
day_df = accessible_log_df[accessible_log_df["date"] == snapshot_date]
total_sales = float(day_df["total"].sum())

day_before = snapshot_date - timedelta(days=1)
day_before_df = accessible_log_df[accessible_log_df["date"] == day_before]
day_before_by_store = day_before_df.set_index("store")["total"].to_dict()

highest_row = day_df.loc[day_df["total"].idxmax()] if not day_df.empty else None
lowest_row = day_df.loc[day_df["total"].idxmin()] if not day_df.empty else None

# Mama's Sold no longer fetches live - see nightly_refresh.py, which
# computes this once a night and saves it here via dashboard_data.py.
mama_date, mama_total, mama_quantity, mama_failed_stores, mama_updated_at = load_mama_snapshot()

mama_note = None
if mama_total is None:
    mama_note = (
        "⚠️ Mama's Sold hasn't been computed yet - run it manually from "
        "Manage Users, or wait for the nightly refresh job."
    )
    mama_total, mama_quantity = 0.0, 0.0
elif mama_failed_stores:
    failed_names = ", ".join(stores[k].get("name", k) for k in mama_failed_stores if k in stores)
    mama_note = f"⚠️ Mama's Sold couldn't be fetched for: {failed_names} - excluded from the total."

# --- Month-to-date pace, company-wide, compared against last month's actual ---
today = store_local_today()
mtd_projected_total = sum(
    compute_pace(accessible_log_df, store_key, today)["projected_monthly"]
    for store_key in store_keys
)
last_month_end = today.replace(day=1) - timedelta(days=1)
last_month_actual_total = sum(
    month_actual_total(accessible_log_df, store_key, last_month_end.year, last_month_end.month)
    for store_key in store_keys
)
mtd_change = pct_change(mtd_projected_total, last_month_actual_total)

month_start = today.replace(day=1)
mtd_window = accessible_log_df[(accessible_log_df["date"] >= month_start) & (accessible_log_df["date"] <= snapshot_date)]
mtd_cumulative = mtd_window.groupby("date")["total"].sum().sort_index().cumsum().tolist()

# --- KPI cards ---
cols = st.columns(5)

with cols[0]:
    spark = sparkline_svg(daily_series(accessible_log_df, snapshot_date, SPARK_DAYS), "#22C55E")
    st.markdown(
        render_metric_card(
            "TOTAL SALES", f"${total_sales:,.2f}",
            icon="$", icon_color="#22C55E", sparkline_html=spark,
        ),
        unsafe_allow_html=True,
    )

with cols[1]:
    if highest_row is not None:
        spark = sparkline_svg(
            daily_series(accessible_log_df, snapshot_date, SPARK_DAYS, store_key=highest_row["store"]), "#3B82F6"
        )
        st.markdown(
            render_metric_card(
                "HIGHEST STORE", store_names.get(highest_row["store"], highest_row["store"]),
                f"${highest_row['total']:,.2f}",
                delta_positive=None, icon="\u25b2", icon_color="#3B82F6", sparkline_html=spark,
            ),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(render_metric_card("HIGHEST STORE", "—", icon="\u25b2", icon_color="#3B82F6"), unsafe_allow_html=True)

with cols[2]:
    if lowest_row is not None:
        spark = sparkline_svg(
            daily_series(accessible_log_df, snapshot_date, SPARK_DAYS, store_key=lowest_row["store"]), "#EF4444"
        )
        st.markdown(
            render_metric_card(
                "LOWEST STORE", store_names.get(lowest_row["store"], lowest_row["store"]),
                f"${lowest_row['total']:,.2f}",
                delta_positive=None, icon="\u25bc", icon_color="#EF4444", sparkline_html=spark,
            ),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(render_metric_card("LOWEST STORE", "—", icon="\u25bc", icon_color="#EF4444"), unsafe_allow_html=True)

with cols[3]:
    mama_period = mama_date.strftime("%b %d") if mama_date else None
    st.markdown(
        render_metric_card(
            "MAMA'S SOLD", f"{mama_quantity:,.0f} units", f"${mama_total:,.2f}", mama_period,
            delta_positive=None, icon="M", icon_color="#A855F7",
        ),
        unsafe_allow_html=True,
    )

with cols[4]:
    spark = sparkline_svg(mtd_cumulative, "#F59E0B")
    st.markdown(
        render_metric_card(
            "MTD PACE", f"${mtd_projected_total:,.0f}",
            pct_delta_text(mtd_change), "vs last month",
            delta_positive=(mtd_change >= 0) if mtd_change is not None else None,
            icon="\u2197", icon_color="#F59E0B", sparkline_html=spark,
        ),
        unsafe_allow_html=True,
    )

if mama_note:
    st.caption(mama_note)

st.write("")

# --- Monthly trend chart + Top Performing Stores, side by side ---
MONTHS_BACK = 6
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

chart_col, ranking_col = st.columns([2, 1])

with chart_col:
    chart_container = st.container(border=True)
    with chart_container:
        title_col, metric_col, popover_col = st.columns([2.4, 1, 0.9])
        with title_col:
            st.markdown(
                '<div style="font-size:1rem; font-weight:600;">Monthly Sales Trend</div>'
                '<div style="font-size:0.8rem; color:#9CA3AF; margin-bottom:0.75rem;">'
                'Compare sales across all stores</div>',
                unsafe_allow_html=True,
            )
        with metric_col:
            # Only Total Sales exists in the pace log (no unit/transaction
            # counts tracked there) - shown as a static label rather than
            # fake Units Sold/Transactions tabs that would do nothing.
            st.markdown(
                '<div style="text-align:right;"><span style="background:#6366F133; color:#A5B4FC; '
                'font-size:0.78rem; padding:5px 12px; border-radius:8px;">Total Sales</span></div>',
                unsafe_allow_html=True,
            )
        with popover_col:
            st.markdown('<div style="height: 4px;"></div>', unsafe_allow_html=True)

        if trend_df.empty:
            st.write("No historical data in the pace log sheet yet for this range.")
        else:
            with popover_col:
                with st.popover("Stores", use_container_width=True):
                    selected_names = st.multiselect(
                        "Stores to show",
                        options=sorted(store_names.values()),
                        default=sorted(store_names.values()),
                        label_visibility="collapsed",
                    )

            trend_df["month"] = trend_df["date"].apply(lambda d: d.strftime("%b %Y"))
            trend_df["store_name"] = trend_df["store"].map(store_names)
            trend_df = trend_df[trend_df["store_name"].isin(selected_names)]

            if trend_df.empty:
                st.write("No stores selected.")
            else:
                monthly = trend_df.groupby(["month", "store_name"], as_index=False)["total"].sum()
                monthly["month"] = pd.Categorical(monthly["month"], categories=month_order, ordered=True)
                monthly = monthly.sort_values("month")

                chart = (
                    alt.Chart(monthly)
                    .mark_line(strokeWidth=2.5, point=alt.OverlayMarkDef(size=40))
                    .encode(
                        x=alt.X("month:N", sort=month_order, title=None,
                                axis=alt.Axis(labelAngle=0, grid=False)),
                        y=alt.Y("total:Q", title=None,
                                axis=alt.Axis(gridColor="rgba(255,255,255,0.08)", format="$,.0f")),
                        color=alt.Color("store_name:N", title=None,
                                         scale=alt.Scale(scheme="tableau10"),
                                         legend=alt.Legend(orient="bottom", columns=4, symbolType="stroke")),
                        tooltip=["store_name", "month", alt.Tooltip("total:Q", format="$,.2f")],
                    )
                    .properties(height=320)
                    .configure_view(strokeWidth=0)
                    .configure_axis(labelColor="#9CA3AF", titleColor="#9CA3AF")
                )
                st.altair_chart(chart, use_container_width=True)

with ranking_col:
    ranking_container = st.container(border=True)
    with ranking_container:
        st.markdown(
            '<div style="font-size:1rem; font-weight:600;">Top Performing Stores</div>'
            f'<div style="font-size:0.8rem; color:#9CA3AF; margin-bottom:0.9rem;">'
            f'By total sales ({snapshot_date.strftime("%b %d")})</div>',
            unsafe_allow_html=True,
        )

        ranked = day_df.sort_values("total", ascending=False).reset_index(drop=True)
        rows_html = []
        for i, row in ranked.iterrows():
            store_key = row["store"]
            name = store_names.get(store_key, store_key)
            change = pct_change(row["total"], day_before_by_store.get(store_key))
            if change is None:
                change_html = ""
            else:
                arrow = "\u25b2" if change >= 0 else "\u25bc"
                color = "#22C55E" if change >= 0 else "#EF4444"
                change_html = f'<span style="color:{color}; font-size:0.78rem;">{arrow} {abs(change):.1f}%</span>'
            rows_html.append(
                f'<div style="display:flex; justify-content:space-between; align-items:center; '
                f'padding:7px 0; border-top:1px solid rgba(255,255,255,0.06);">'
                f'<div style="display:flex; align-items:center; gap:8px; min-width:0;">'
                f'<span style="color:#6B7280; font-size:0.78rem; width:16px;">{i + 1}</span>'
                f'<span style="font-size:0.85rem; overflow:hidden; text-overflow:ellipsis; '
                f'white-space:nowrap;">{name}</span></div>'
                f'<div style="display:flex; align-items:center; gap:8px; flex-shrink:0;">'
                f'<span style="font-size:0.85rem; font-weight:500;">${row["total"]:,.0f}</span>'
                f'{change_html}</div></div>'
            )
        st.markdown("".join(rows_html), unsafe_allow_html=True)

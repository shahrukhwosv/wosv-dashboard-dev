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
live on page load, which is deliberate (nobody should have to wait on a
Lightspeed fetch just to open the homepage). If nobody's run that job
recently, numbers here can be stale - there's a caption below showing the
most recent date actually found in the sheet so that's visible at a
glance.

Mama's Sold specifically comes from dashboard_data.load_mama_snapshot() -
nightly_refresh.py computes it once a night (the pace log sheet only
tracks each store's total dollars per day, not a category/keyword
breakdown, so it can't be read from there like the other metrics). It's
also the one metric that intentionally covers all connected stores
(unrestricted - the same scope Category Sales uses), not just the
Standard Stores list the rest of the page uses. It has no day-over-day
comparison shown - the nightly job only keeps the latest day's snapshot,
not a running history, so there's nothing to compare against without
fabricating a number.

MTD Pace reuses sales_pace.compute_pace() (same projection math as the
Pace Calculator page) per store, summed into one company-wide projected
total for the current month, compared against last month's actual total
(sales_pace.month_actual_total(), also already existing) - both are real
values, not invented trend data.

Total Sales and the Top Performing Stores ranking both show a real
day-over-day comparison, computed from the same log data already being
read for the rest of the page (no new data source needed for this).

Everything except Mama's Sold is restricted to the Standard Stores list,
same as Commissions/Transactions/Touch Tell/Monthly Reports/Purchase
Order Status.
"""

from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

import lightspeed_client as ls
from dashboard_data import load_mama_snapshot
from sales_pace import compute_pace, month_actual_total, read_daily_log, store_local_today
from store_access import DEFAULT_STANDARD_STORE_KEYS, STANDARD_STORES_LIST, get_page_store_keys

CARD_STYLE = (
    "background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); "
    "border-radius: 12px;"
)


def render_metric_card(label, value, delta_text=None, delta_positive=None, icon="•", icon_color="#6366F1"):
    """Renders one KPI card as raw HTML. delta_positive: True (green),
    False (red), or None (neutral gray) - pass None when there's no real
    comparison to show, rather than fabricating a direction."""
    delta_html = ""
    if delta_text:
        color = "#22C55E" if delta_positive is True else "#EF4444" if delta_positive is False else "#9CA3AF"
        delta_html = f'<div style="font-size:0.8rem; color:{color}; margin-top:4px;">{delta_text}</div>'
    return f"""
    <div style="{CARD_STYLE} padding: 1rem 1.1rem; height: 128px;">
        <div style="display:flex; align-items:center; gap:8px; margin-bottom:0.6rem;">
            <div style="width:26px; height:26px; border-radius:8px; background:{icon_color}22;
                        color:{icon_color}; display:flex; align-items:center; justify-content:center;
                        font-size:0.85rem; font-weight:600;">{icon}</div>
            <div style="font-size:0.78rem; color:#9CA3AF;">{label}</div>
        </div>
        <div style="font-size:1.45rem; font-weight:600; line-height:1.2;">{value}</div>
        {delta_html}
    </div>
    """


def pct_change(current, previous):
    if not previous:
        return None
    return (current - previous) / previous * 100


config = ls.load_config()
stores = config["stores"]

store_keys = list(get_page_store_keys(
    config, list_name=STANDARD_STORES_LIST, default_keys=DEFAULT_STANDARD_STORE_KEYS
))
store_names = {key: stores[key].get("name", key) for key in store_keys}

st.title("Dashboard")

if not store_keys:
    st.info("No stores available. Check the Standard Stores list on the Manage Users page.")
    st.stop()

st.caption(f"Here's what's happening across your {len(store_keys)} store{'s' if len(store_keys) != 1 else ''}.")

log_df = read_daily_log()
accessible_log_df = log_df[log_df["store"].isin(store_keys)]

if accessible_log_df.empty:
    st.warning(
        "No data found in the pace log sheet yet for your stores. Visit "
        "the Pace Calculator page and click 'Fetch missing days from "
        "Lightspeed' to populate it, or wait for the nightly refresh job."
    )
    st.stop()

most_recent_date = accessible_log_df["date"].max()
yesterday = store_local_today() - timedelta(days=1)
if most_recent_date < yesterday:
    st.warning(
        f"⚠️ The pace log sheet's most recent data is from "
        f"{most_recent_date.strftime('%B %d')} - nightly_refresh.py may not "
        f"have run recently. Numbers below may be out of date."
    )

# --- Yesterday's totals (or the most recent day actually in the sheet) ---
snapshot_date = min(yesterday, most_recent_date)
day_df = accessible_log_df[accessible_log_df["date"] == snapshot_date]
total_sales = float(day_df["total"].sum())

day_before = snapshot_date - timedelta(days=1)
day_before_df = accessible_log_df[accessible_log_df["date"] == day_before]
day_before_total = float(day_before_df["total"].sum())
day_before_by_store = day_before_df.set_index("store")["total"].to_dict()

total_sales_change = pct_change(total_sales, day_before_total)

highest_row = day_df.loc[day_df["total"].idxmax()] if not day_df.empty else None
lowest_row = day_df.loc[day_df["total"].idxmin()] if not day_df.empty else None

st.caption(f"Showing {snapshot_date.strftime('%A, %B %d')}")

# Mama's Sold no longer fetches live - see nightly_refresh.py, which
# computes this once a night and saves it here via dashboard_data.py.
mama_date, mama_total, mama_quantity, mama_failed_stores, mama_updated_at = load_mama_snapshot()

mama_note = None
if mama_total is None:
    mama_note = (
        "⚠️ Mama's Sold hasn't been computed yet - the nightly refresh job "
        "hasn't run for the first time. See nightly_refresh.py for setup."
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

# --- KPI cards ---
cols = st.columns(5)
with cols[0]:
    delta = f"{'+' if total_sales_change and total_sales_change >= 0 else ''}{total_sales_change:.1f}% vs prior day" if total_sales_change is not None else None
    st.markdown(
        render_metric_card(
            "TOTAL SALES", f"${total_sales:,.2f}", delta,
            delta_positive=(total_sales_change >= 0) if total_sales_change is not None else None,
            icon="$", icon_color="#6366F1",
        ),
        unsafe_allow_html=True,
    )
with cols[1]:
    name = store_names.get(highest_row["store"], highest_row["store"]) if highest_row is not None else "—"
    val = f"${highest_row['total']:,.2f}" if highest_row is not None else None
    st.markdown(
        render_metric_card("HIGHEST STORE", name, val, delta_positive=None, icon="\u25b2", icon_color="#22C55E"),
        unsafe_allow_html=True,
    )
with cols[2]:
    name = store_names.get(lowest_row["store"], lowest_row["store"]) if lowest_row is not None else "—"
    val = f"${lowest_row['total']:,.2f}" if lowest_row is not None else None
    st.markdown(
        render_metric_card("LOWEST STORE", name, val, delta_positive=None, icon="\u25bc", icon_color="#EF4444"),
        unsafe_allow_html=True,
    )
with cols[3]:
    mama_delta = f"${mama_total:,.2f}" + (f" ({mama_date.strftime('%b %d')})" if mama_date else "")
    st.markdown(
        render_metric_card(
            "MAMA'S SOLD", f"{mama_quantity:,.0f} units", mama_delta,
            delta_positive=None, icon="M", icon_color="#A855F7",
        ),
        unsafe_allow_html=True,
    )
with cols[4]:
    delta = f"{'+' if mtd_change and mtd_change >= 0 else ''}{mtd_change:.1f}% vs last month" if mtd_change is not None else "Projected this month"
    st.markdown(
        render_metric_card(
            "MTD PACE", f"${mtd_projected_total:,.0f}", delta,
            delta_positive=(mtd_change >= 0) if mtd_change is not None else None,
            icon="\u2197", icon_color="#F59E0B",
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
    st.markdown(f'<div style="{CARD_STYLE} padding: 1.25rem;">', unsafe_allow_html=True)
    header_col, popover_col = st.columns([3, 1])
    with header_col:
        st.markdown(
            '<div style="font-size:1rem; font-weight:600;">Monthly Sales Trend</div>'
            '<div style="font-size:0.8rem; color:#9CA3AF; margin-bottom:0.75rem;">'
            'Compare sales across all stores</div>',
            unsafe_allow_html=True,
        )

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
            monthly = (
                trend_df.groupby(["month", "store_name"], as_index=False)["total"].sum()
            )
            monthly["month"] = pd.Categorical(monthly["month"], categories=month_order, ordered=True)
            monthly = monthly.sort_values("month")

            chart = (
                alt.Chart(monthly)
                .mark_line(strokeWidth=2.5, point=alt.OverlayMarkDef(size=45))
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

    st.markdown("</div>", unsafe_allow_html=True)

with ranking_col:
    st.markdown(f'<div style="{CARD_STYLE} padding: 1.25rem; height: 100%;">', unsafe_allow_html=True)
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
    st.markdown("</div>", unsafe_allow_html=True)

"""
Main Dashboard page - the same editorial look as the Morning Brief email
(morning_brief.py): a serif greeting, then three numbered brand cards.

  01 RETAIL (World of Smoke & Vape) - yesterday's sales, highest/lowest
     store, South and North monthly pace, a 6-month sales chart (All /
     North / South), every store's yesterday + monthly pace, and
     yesterday's DoorDash orders.
     -> pace log Google Sheet (sales_pace.read_daily_log)
     -> dashboard database (dashboard_data.load_doordash_snapshot)
  02 WHOLESALE (Top Shelf Novelties) - month-to-date revenue, profit,
     margin and invoice count, plus yesterday's invoices with a totals row.
     Only shown to people who can see the Top Shelf Invoices page.
     -> "Top Shelf Invoices" sheet tab (topshelf_invoices.read_log)
  03 MANUFACTURING (Mama's) - yesterday's units and sales across stores,
     yesterday's RepRally cases, and RepRally cases month to date by flavor.
     -> dashboard database (dashboard_data.load_mama_snapshot)
     -> RepRally brand portal (reprally_client.py, cached for an hour;
        needs REPRALLY_USERNAME / REPRALLY_PASSWORD on the web service,
        otherwise that part just says it's unavailable)

Nothing here calls Lightspeed live - it reads what nightly_refresh.py saved.
Retail and DoorDash use the Pace Calculator Stores list (editable from
Manage Users); Mama's Sold covers every connected store.

The cards are keyed st.container()s styled with CSS (.st-key-card_*), so
real Streamlit widgets (the chart and its region picker) can sit inside
them. Card content is plain HTML written with st.html() - not
st.markdown() - so dollar signs never get read as LaTeX math.

Colors match morning_brief.py's WOSV / TSN / MAMAS palettes; the overall
light theme lives in .streamlit/config.toml.
"""

from datetime import date, datetime, timedelta
from html import escape

import altair as alt
import pandas as pd
import streamlit as st

import lightspeed_client as ls
import reprally_client
from dashboard_data import load_doordash_snapshot, load_mama_snapshot
from page_access import get_accessible_pages
from sales_pace import STORE_TIMEZONE, compute_pace, read_daily_log, store_local_today
from store_access import PACE_CALCULATOR_STORES_LIST, get_page_store_keys
from store_regions import NORTH_STORES, SOUTH_STORES
from topshelf_invoices import read_log as read_tsn_log

ERP_URL = "https://erp.topshelfnovelties.com"

# Same palettes as morning_brief.py (text shades darkened where needed so
# small labels stay readable on white).
WOSV = {"accent": "#1ab0e6", "text": "#0b6f96", "border": "#b3e3f6", "tile": "#e8f7fd",
        "btn_bg": "#cdeefa", "btn_fg": "#0b6f96", "bar": "#9fdcf3"}
TSN = {"accent": "#f8636b", "navy": "#154859", "border": "#c5d5dc", "tile": "#eef3f5"}
MAMAS = {"red": "#e0283a", "text": "#c41e2f", "border": "#f3c3c7", "tile": "#f4f2ee"}

SERIF = "Georgia, 'Times New Roman', serif"
GRAY = "#6b7280"

# Logos: the same files the email uses.
LOGOS = {
    "wosv": "assets/email/wosv_logo.png",
    "tsn": "assets/email/tsn_logo.png",
    "mamas": "assets/email/mamas_logo.png",
}

CSS = f"""
<style>
.stMainBlockContainer {{ max-width: 1240px; }}
h1, h2, h3 {{ font-family: {SERIF} !important; }}
.st-key-card_retail, .st-key-card_tsn, .st-key-card_mamas {{
  background: #ffffff; border-radius: 12px; padding: 22px; box-sizing: border-box;
}}
.st-key-card_retail {{ border: 1px solid {WOSV["border"]}; }}
.st-key-card_tsn {{ border: 1px solid {TSN["border"]}; }}
.st-key-card_mamas {{ border: 1px solid {MAMAS["border"]}; }}
/* Wholesale and Mama's sit side by side at the same height. */
div[data-testid="stHorizontalBlock"]:has(.st-key-card_tsn):has(.st-key-card_mamas) {{ align-items: stretch !important; }}
div[data-testid="stColumn"]:has(.st-key-card_tsn), div[data-testid="stColumn"]:has(.st-key-card_mamas) {{ align-self: stretch; }}
div[data-testid="stColumn"] div:has(.st-key-card_tsn), div[data-testid="stColumn"] div:has(.st-key-card_mamas),
.st-key-card_tsn, .st-key-card_mamas {{ height: 100%; }}
.wb-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.wb-table th {{ font-size: 10px; letter-spacing: 1px; color: {GRAY}; font-weight: 600; text-align: left;
  padding: 8px 4px; border-bottom: 1px solid #d1d5db; }}
.wb-table td {{ padding: 9px 4px; border-bottom: 1px solid #eceef2; }}
.wb-table .r {{ text-align: right; }}
.wb-table tr.total td {{ font-weight: 800; border-bottom: 0; border-top: 1px solid #d1d5db; }}
.wb-label {{ font-size: 11px; letter-spacing: 1.5px; color: {GRAY}; font-weight: 700; margin-bottom: 4px; }}
.wb-btn {{ display: inline-block; text-decoration: none !important; font-size: 14px; font-weight: 700;
  padding: 12px 18px; border-radius: 8px; white-space: nowrap; }}
</style>
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def money0(v):
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def money2(v):
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def short_day(d):
    """'OCT 25' - built by hand since %-d doesn't work on Windows."""
    return f"{d:%b} {d.day}".upper()


def pct_change(current, previous):
    if not previous:
        return None
    return (current - previous) / previous * 100


def can_see(page_key):
    """Same rule app.py uses for the sidebar: admins see everything; a user
    with no saved page grants yet sees every page; otherwise only granted
    pages."""
    if st.session_state.get("is_admin"):
        return True
    try:
        granted = get_accessible_pages(st.session_state.user_id)
    except Exception:
        return False
    return (not granted) or (page_key in granted)


def html(s):
    """st.html with the indentation stripped, so the source can be written
    readably."""
    st.html("".join(line.strip() for line in s.splitlines()))


def card_header(num, label, label_color, bar_color, subtitle, button=""):
    button_html = f'<div style="align-self:center;">{button}</div>' if button else ""
    return f'''
    <div style="display:flex; gap:14px; align-items:stretch; flex-wrap:wrap; font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;">
      <div style="width:6px; background:{bar_color}; border-radius:3px;"></div>
      <div style="flex:1 1 220px;">
        <div style="font-size:12px; letter-spacing:1.5px; font-weight:700;">
          <span style="color:{GRAY};">{num}</span><span style="color:#cbd0da; padding:0 10px;">|</span><span style="color:{label_color};">{label}</span>
        </div>
        <div style="font-size:14px; color:{GRAY}; margin-top:6px;">{subtitle}</div>
      </div>
      {button_html}
    </div>'''


def tiles(items, bg, sub_color, min_width=150):
    """items: list of (label, value, sub)."""
    cells = "".join(
        f'<div style="background:{bg}; border-radius:8px; padding:14px 16px; min-width:0;">'
        f'<div style="font-size:11px; letter-spacing:1.5px; color:#4b5563; font-weight:700;">{label}</div>'
        f'<div style="font-size:24px; font-weight:800; margin-top:4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{value}</div>'
        + (f'<div style="font-size:12px; color:{sub_color}; font-weight:600; margin-top:2px;">{sub}</div>' if sub else "")
        + '</div>'
        for label, value, sub in items
    )
    return (f'<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax({min_width}px, 1fr)); '
            f'gap:10px;">{cells}</div>')


def empty_line(text):
    return f'<div style="font-size:13px; color:{GRAY}; padding:8px 0;">{text}</div>'


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@st.cache_data(ttl=600)
def load_tsn_log():
    return read_tsn_log()


@st.cache_data(ttl=3600, show_spinner=False)
def load_reprally(month_start, yesterday):
    """RepRally is a live login, so it's cached for an hour. Returns None
    when it isn't set up on this service, or {"error": ...} if it fails."""
    if not reprally_client.is_configured():
        return None
    try:
        rr = reprally_client.RepRallyClient()
        mtd = rr.cases_by_flavor(month_start, yesterday)
        return {
            "yesterday_cases": sum(rr.cases_by_flavor(yesterday, yesterday).values()),
            "mtd": list(mtd.items()),
            "mtd_total": sum(mtd.values()),
        }
    except Exception as e:
        return {"error": str(e)}


st.html(CSS)

config = ls.load_config()
stores = config["stores"]

store_keys = list(get_page_store_keys(
    config,
    list_name=PACE_CALCULATOR_STORES_LIST,
    default_keys=list(stores.keys()),  # matches pace_calculator_page.py's own default
    require_connected=False,  # matches pace_calculator_page.py - every store key in the list, connected or not
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

today = store_local_today()
yesterday = today - timedelta(days=1)
most_recent_date = accessible_log_df["date"].max()
snapshot_date = min(yesterday, most_recent_date)
snap_label = short_day(snapshot_date)

# --- Retail numbers ---
day_df = accessible_log_df[accessible_log_df["date"] == snapshot_date]
total_sales = float(day_df["total"].sum())
by_store = day_df.groupby("store")["total"].sum()
day_before_df = accessible_log_df[accessible_log_df["date"] == snapshot_date - timedelta(days=1)]
day_before_by_store = day_before_df.groupby("store")["total"].sum().to_dict()

paces = {k: compute_pace(accessible_log_df, k, today)["projected_monthly"] for k in store_keys}
south_pace = sum(v for k, v in paces.items() if store_names[k] in SOUTH_STORES)
north_pace = sum(v for k, v in paces.items() if store_names[k] in NORTH_STORES)
total_pace = sum(paces.values())

store_rows = sorted(
    ((k, float(by_store.get(k, 0.0))) for k in store_keys),
    key=lambda x: (-x[1], store_names[x[0]]),
)

# --- DoorDash (same stores) ---
dd_date, dd_stores, dd_failed = load_doordash_snapshot()
dd_note = None
if dd_date != snapshot_date:
    dd_note = ("DoorDash sales haven't been saved yet." if dd_date is None
               else f"DoorDash sales are from {dd_date:%b} {dd_date.day}, not {snapshot_date:%b} {snapshot_date.day}.")
    dd_stores = []
listed_names = set(store_names.values())
dd_stores = [s for s in (dd_stores or []) if s["name"] in listed_names]
dd_with_orders = sorted((s for s in dd_stores if s["orders"] > 0), key=lambda s: (-s["sales"], s["name"]))

# --- Mama's ---
mama_date, mama_total, mama_quantity, mama_failed_stores, _ = load_mama_snapshot()
mama_note = None
if mama_total is None:
    mama_note = ("Mama's Sold hasn't been computed yet - run it from Manage Users, "
                 "or wait for the nightly refresh job.")
    mama_total, mama_quantity = 0.0, 0.0
elif mama_failed_stores:
    failed = ", ".join(stores[k].get("name", k) for k in mama_failed_stores if k in stores)
    mama_note = f"Mama's Sold couldn't be fetched for: {failed} - excluded from the total."

# --- Top Shelf (wholesale) ---
show_tsn = can_see("topshelf_invoices")
tsn, tsn_error = None, None
tsn_month_start = yesterday.replace(day=1)
if show_tsn:
    try:
        tsn_log = load_tsn_log()
        mtd = tsn_log[(tsn_log["Date"] >= tsn_month_start) & (tsn_log["Date"] <= yesterday)]
        day = tsn_log[tsn_log["Date"] == yesterday].sort_values("Invoice #")
        mtd_sales, mtd_profit = float(mtd["Product Sales"].sum()), float(mtd["Profit"].sum())
        day_sales, day_profit = float(day["Product Sales"].sum()), float(day["Profit"].sum())
        tsn = {
            "revenue": float(mtd["Amount"].sum()),
            "profit": mtd_profit,
            "margin": (mtd_profit / mtd_sales * 100) if mtd_sales > 0 else None,
            "count": int(len(mtd)),
            "rows": day.to_dict("records"),
            "tot_amount": float(day["Amount"].sum()),
            "tot_shipping": float(day["Shipping"].sum()),
            "tot_profit": day_profit,
            "tot_margin": (day_profit / day_sales * 100) if day_sales > 0 else None,
        }
    except Exception as e:
        tsn_error = str(e)


# ---------------------------------------------------------------------------
# Masthead + greeting
# ---------------------------------------------------------------------------

now = datetime.now(STORE_TIMEZONE)
part_of_day = "morning" if now.hour < 12 else "afternoon" if now.hour < 17 else "evening"
first_name = str(st.session_state.get("username", "")).split("@")[0].split(".")[0].split("_")[0].capitalize()
greeting = f"Good {part_of_day}, {escape(first_name)}" if first_name else f"Good {part_of_day}"

html(f'''
<div style="font-family:{SERIF}; font-size:13px; letter-spacing:3px; color:#111827; text-transform:uppercase;
            padding-bottom:14px; border-bottom:1px solid #cbd0da; display:flex; align-items:center;">
  <span style="display:inline-block; background:{WOSV["accent"]}; color:#ffffff; font-size:14px; letter-spacing:0;
               padding:3px 8px; margin-right:10px;">W</span>WOSV Dashboard
</div>
<div style="display:flex; align-items:flex-end; justify-content:space-between; gap:16px; flex-wrap:wrap; margin-top:22px;">
  <div>
    <div style="font-family:{SERIF}; font-size:44px; line-height:1.1; font-weight:700; color:#0b0f19;">{greeting}</div>
    <div style="font-family:{SERIF}; font-size:18px; color:#111827; margin-top:10px;">{today:%A, %B} {today.day}, {today.year}</div>
  </div>
  <div style="font-size:12px; color:#4b5563; background:#ffffff; border:1px solid #e5e3dc; border-radius:8px; padding:8px 12px;
              font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;">
    Data through {snapshot_date:%a, %b} {snapshot_date.day} &middot; {len(store_keys)} store{"s" if len(store_keys) != 1 else ""}
  </div>
</div>
''')

if most_recent_date < yesterday:
    st.warning(
        f"Heads up: the pace log sheet only goes through {most_recent_date:%B} {most_recent_date.day} - "
        f"the nightly refresh may not have run. Numbers below may be out of date."
    )


# ---------------------------------------------------------------------------
# 01 RETAIL
# ---------------------------------------------------------------------------

with st.container(key="card_retail"):
    pace_button = ""
    if can_see("pace_calculator"):
        pace_button = (f'<a class="wb-btn" href="/pace_calculator_page" target="_self" '
                       f'style="background:{WOSV["btn_bg"]}; color:{WOSV["btn_fg"]};">Open Pace Calculator &rarr;</a>')
    html(card_header("01", "RETAIL", WOSV["text"], WOSV["accent"],
                     f"Yesterday &middot; {snap_label.title()}", pace_button))
    st.image(LOGOS["wosv"], width=230)

    if day_df.empty:
        high = low = None
    else:
        high = day_df.loc[day_df["total"].idxmax()]
        low = day_df.loc[day_df["total"].idxmin()]
    html(tiles([
        ("SALES", money0(total_sales), "All stores"),
        ("HIGHEST STORE", money0(high["total"]) if high is not None else "&mdash;",
         escape(store_names.get(high["store"], high["store"])) if high is not None else ""),
        ("LOWEST STORE", money0(low["total"]) if low is not None else "&mdash;",
         escape(store_names.get(low["store"], low["store"])) if low is not None else ""),
        ("SOUTH PACE", money0(south_pace), f"South stores &middot; {today:%b}"),
        ("NORTH PACE", money0(north_pace), f"North stores &middot; {today:%b}"),
    ], WOSV["tile"], WOSV["text"], min_width=160))

    chart_col, table_col = st.columns([3, 2], gap="large")

    with chart_col:
        label_col, pick_col = st.columns([1, 1], vertical_alignment="center")
        with label_col:
            html('<div class="wb-label" style="margin:0;">MONTHLY SALES</div>')
        with pick_col:
            region = st.segmented_control(
                "Stores", ["All", "North", "South"], default="All",
                key="dashboard_region", label_visibility="collapsed",
            ) or "All"
        if region == "North":
            region_keys = [k for k in store_keys if store_names[k] in NORTH_STORES]
        elif region == "South":
            region_keys = [k for k in store_keys if store_names[k] in SOUTH_STORES]
        else:
            region_keys = store_keys

        # Last 6 months; the current month shows month-to-date (solid) inside
        # its projected pace (dashed outline).
        months = []
        y, m = today.year, today.month
        for _ in range(6):
            months.append(date(y, m, 1))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        months.reverse()
        region_df = accessible_log_df[accessible_log_df["store"].isin(region_keys)]
        rows = []
        for ms in months:
            in_month = region_df[(region_df["date"] >= ms) &
                                 (region_df["date"] < (ms + timedelta(days=32)).replace(day=1))]
            actual = float(in_month["total"].sum())
            is_current = ms.year == today.year and ms.month == today.month
            pace = sum(paces.get(k, 0.0) for k in region_keys) if is_current else None
            top = max(actual, pace or 0.0)
            rows.append({
                "month": ms.strftime("%b").upper(),
                "actual": actual,
                "pace": pace,
                "top": top,
                "label": f"${top / 1_000_000:.2f}M" if top >= 1_000_000 else f"${top / 1000:,.0f}K",
                "current": "current" if is_current else "past",
            })
        chart_df = pd.DataFrame(rows)
        order = list(chart_df["month"])
        x = alt.X("month:N", sort=order, title=None,
                  axis=alt.Axis(labelAngle=0, domainColor="#d1d5db", ticks=False, labelColor=GRAY,
                                labelFontSize=11, labelPadding=8))
        bars = alt.Chart(chart_df).mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6, size=46).encode(
            x=x,
            y=alt.Y("actual:Q", title=None, axis=None, scale=alt.Scale(domainMin=0)),
            color=alt.Color("current:N", legend=None,
                            scale=alt.Scale(domain=["past", "current"], range=[WOSV["bar"], WOSV["accent"]])),
            tooltip=[alt.Tooltip("month:N", title="Month"), alt.Tooltip("actual:Q", title="Sales", format="$,.0f")],
        )
        pace_outline = alt.Chart(chart_df.dropna(subset=["pace"])).mark_bar(
            cornerRadiusTopLeft=6, cornerRadiusTopRight=6, size=46, fillOpacity=0,
            stroke=WOSV["accent"], strokeWidth=2, strokeDash=[5, 4],
        ).encode(x=x, y="pace:Q",
                 tooltip=[alt.Tooltip("month:N", title="Month"), alt.Tooltip("pace:Q", title="Projected", format="$,.0f")])
        labels = alt.Chart(chart_df).mark_text(dy=-10, fontSize=12, fontWeight="bold", color="#111827").encode(
            x=x, y="top:Q", text="label:N")
        st.altair_chart(
            (bars + pace_outline + labels).properties(height=240, background="#ffffff")
            .configure_view(strokeWidth=0),
            width="stretch",
        )
        html(f'<div style="font-size:12px; color:#4b5563;">{today:%B} bar = month to date; dashed outline = projected pace.</div>')

    with table_col:
        body = ""
        for k, sales in store_rows:
            change = pct_change(sales, day_before_by_store.get(k))
            if change is None:
                chg = '<td class="r" style="color:#9ca3af;">&mdash;</td>'
            else:
                color = "#047857" if change >= 0 else "#b91c1c"
                chg = f'<td class="r" style="color:{color}; font-weight:600;">{"+" if change >= 0 else "&minus;"}{abs(change):.1f}%</td>'
            body += (f'<tr><td>{escape(store_names[k])}</td><td class="r">{money0(sales)}</td>{chg}'
                     f'<td class="r">{money0(paces.get(k, 0.0))}</td></tr>')
        html(f'''
        <div class="wb-label">ALL STORES &bull; {snap_label}</div>
        <div style="overflow-x:auto;">
        <table class="wb-table">
          <tr><th>LOCATION</th><th class="r">YESTERDAY</th><th class="r">VS DAY BEFORE</th><th class="r">MONTHLY PACE</th></tr>
          {body}
          <tr class="total"><td>Total</td><td class="r">{money0(total_sales)}</td><td></td><td class="r">{money0(total_pace)}</td></tr>
        </table>
        </div>''')

    # DoorDash
    if dd_note:
        dd_table = empty_line(escape(dd_note))
    elif dd_with_orders:
        dd_table = '<table class="wb-table"><tr><th>LOCATION</th><th class="r">ORDERS</th><th class="r">REVENUE</th></tr>' + "".join(
            f'<tr><td>{escape(s["name"])}</td><td class="r">{s["orders"]:,}</td><td class="r">{money0(s["sales"])}</td></tr>'
            for s in dd_with_orders
        ) + "</table>"
    else:
        dd_table = empty_line("No DoorDash orders yesterday.")
    dd_tiles = tiles([
        ("ORDERS", f'{sum(s["orders"] for s in dd_stores):,}', ""),
        ("ITEMS", f'{sum(s["items"] for s in dd_stores):,.0f}', ""),
        ("SALES", money0(sum(s["sales"] for s in dd_stores)), ""),
    ], WOSV["tile"], WOSV["text"], min_width=90)
    html(f'''
    <div style="border-top:1px solid {WOSV["border"]}; padding-top:18px; display:flex; flex-wrap:wrap; gap:24px; align-items:flex-start;">
      <div style="flex:1 1 280px;">
        <div class="wb-label" style="color:{WOSV["text"]}; margin-bottom:10px;">DOORDASH &bull; {snap_label}</div>
        {dd_tiles}
      </div>
      <div style="flex:1 1 280px; min-width:0;">{dd_table}</div>
    </div>''')


# ---------------------------------------------------------------------------
# 02 WHOLESALE + 03 MANUFACTURING (side by side, same height)
# ---------------------------------------------------------------------------

def render_wholesale():
    with st.container(key="card_tsn"):
        html(card_header("02", "WHOLESALE", TSN["navy"], TSN["accent"], "Month to date",
                         f'<a class="wb-btn" href="{ERP_URL}" target="_blank" '
                         f'style="background:{TSN["navy"]}; color:#ffffff;">View ERP &rarr;</a>'))
        st.image(LOGOS["tsn"], width=230)
        if tsn_error:
            st.warning(f"Couldn't load Top Shelf invoices: {tsn_error}")
            return
        html(tiles([
            ("REVENUE", money0(tsn["revenue"]), ""),
            ("PROFIT", money0(tsn["profit"]), f'{tsn["margin"]:.1f}% margin' if tsn["margin"] is not None else ""),
            ("INVOICES", f'{tsn["count"]:,}', ""),
        ], TSN["tile"], TSN["navy"]))

        if tsn["rows"]:
            body, watch = "", []
            for x in tsn["rows"]:
                neg = x["Profit"] < 0
                margin = "&mdash;" if pd.isna(x["Margin %"]) else f'{x["Margin %"]:.1f}%'
                if neg:
                    watch.append(f'Invoice {int(x["Invoice #"])} ({escape(str(x["Customer"]))}) lost {money2(abs(x["Profit"]))}.')
                body += (
                    ('<tr style="background:#fff7ed;">' if neg else '<tr>')
                    + f'<td style="color:{GRAY};">{int(x["Invoice #"])}</td><td>{escape(str(x["Customer"]))}</td>'
                    f'<td class="r">{money2(x["Amount"])}</td><td class="r">{money2(x["Shipping"])}</td>'
                    f'<td class="r" style="{"color:#b91c1c; font-weight:700;" if neg else ""}">{money2(x["Profit"])}</td>'
                    f'<td class="r" style="color:{"#9ca3af" if margin == "&mdash;" else TSN["navy"]};">{margin}</td></tr>'
                )
            tm = "&mdash;" if tsn["tot_margin"] is None else f'{tsn["tot_margin"]:.1f}%'
            watch_html = "".join(
                f'<div style="font-size:13px; margin-top:10px;"><span style="display:inline-block; width:10px; height:10px; '
                f'border-radius:5px; background:{TSN["accent"]}; margin-right:6px;"></span><strong>Watch:</strong> '
                f'<span style="color:#4b5563;">{w}</span></div>' for w in watch
            )
            table = f'''
            <div style="overflow-x:auto;">
            <table class="wb-table" style="min-width:520px;">
              <tr><th>INVOICE</th><th>CUSTOMER</th><th class="r">AMOUNT</th><th class="r">SHIPPING</th><th class="r">PROFIT</th><th class="r">MARGIN</th></tr>
              {body}
              <tr class="total"><td></td><td>Total</td><td class="r">{money2(tsn["tot_amount"])}</td>
                <td class="r">{money2(tsn["tot_shipping"])}</td><td class="r">{money2(tsn["tot_profit"])}</td><td class="r">{tm}</td></tr>
            </table>
            </div>{watch_html}'''
        else:
            table = empty_line("No invoices yesterday.")
        html(f'<div class="wb-label">YESTERDAY&rsquo;S INVOICES &bull; {short_day(yesterday)}</div>{table}')


def render_manufacturing():
    with st.container(key="card_mamas"):
        sub = "Yesterday &middot; all stores" if mama_date is None else f"{mama_date:%b} {mama_date.day} &middot; all stores"
        html(card_header("03", "MANUFACTURING", MAMAS["text"], MAMAS["red"], sub))
        st.image(LOGOS["mamas"], width=230)

        rr = load_reprally(tsn_month_start, yesterday)
        cases = lambda n: f"{n:,} case" + ("" if n == 1 else "s")
        if rr and "error" not in rr:
            rr_tile = cases(rr["yesterday_cases"])
            flav = "".join(f'<tr><td>{escape(f)}</td><td class="r">{cases(c)}</td></tr>' for f, c in rr["mtd"])
            rr_table = (f'<table class="wb-table"><tr><th>FLAVOR</th><th class="r">CASES</th></tr>{flav}'
                        f'<tr class="total"><td>Total</td><td class="r">{cases(rr["mtd_total"])}</td></tr></table>')
        else:
            rr_tile = "&mdash;"
            rr_table = empty_line("RepRally isn't connected on this service." if rr is None
                                  else "RepRally numbers couldn't be loaded.")
        html(tiles([
            ("UNITS", f"{mama_quantity:,.0f}", ""),
            ("SALES", money0(mama_total), ""),
            ("REPRALLY", rr_tile, ""),
        ], MAMAS["tile"], MAMAS["text"], min_width=90))
        html(f'<div class="wb-label">REPRALLY ORDERS &bull; MONTH TO DATE</div>{rr_table}')
        if mama_note:
            st.caption(mama_note)


st.write("")
if show_tsn:
    tsn_col, mamas_col = st.columns([3, 2], gap="medium")
    with tsn_col:
        render_wholesale()
    with mamas_col:
        render_manufacturing()
else:
    render_manufacturing()

html(f'''
<div style="border-top:1px solid #cbd0da; padding-top:14px; margin-top:12px; font-size:11px; color:{GRAY};">
  Reporting period: 12:00 AM&ndash;11:59 PM CT &middot; Refreshed nightly at 3 AM
</div>''')

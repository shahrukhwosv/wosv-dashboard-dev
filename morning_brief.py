"""
Morning Brief email - run once each morning as its own Railway Cron Job
(a separate service from the web app and from nightly_refresh.py), after
the 3 AM nightly refresh has filled in yesterday's data.

WHAT'S IN IT (all read from data the dashboard already keeps - this script
makes no Lightspeed/ERP calls of its own):
  01 Retail (World of Smoke & Vape): yesterday's total sales, South and
     North store pace (projected month totals, same math and same store
     groups as the Pace Calculator page), and the top 5 stores by
     yesterday's sales with each one's monthly pace.
     -> pace log Google Sheet (sales_pace.read_daily_log)
  02 Wholesale (Top Shelf Novelties): month-to-date revenue, profit,
     margin and invoice count, plus yesterday's invoices with a totals row.
     -> "Top Shelf Invoices" sheet tab (topshelf_invoices.read_log)
  03 Manufacturing (Mama's): yesterday's units and sales across stores.
     -> dashboard database (dashboard_data.load_mama_snapshot)

Each recipient gets their own copy so the greeting can use their name.
Logos are attached inline (cid: images) rather than linked, so they show
even when a mail app blocks remote images. Each logo image includes its
own rounded white container - drawn into the PNG rather than with CSS -
because the Gmail app darkens every background color in dark mode but
never alters images, so the white container stays white.

SENDING: Railway blocks outgoing SMTP (email ports) on its Free/Hobby
plans, so the email is handed to a small Google Apps Script web app over
HTTPS, which sends it from your Google Workspace Gmail (MailApp). The Apps
Script code is in send_mail_apps_script.js - see that file for setup.
(If you ever move to Railway Pro, direct Gmail SMTP also works: set
GMAIL_ADDRESS + GMAIL_APP_PASSWORD and leave MAIL_WEBHOOK_URL unset.)

ENVIRONMENT VARIABLES (Railway):
  MAIL_WEBHOOK_URL     the Apps Script web app URL (ends in /exec)
  MAIL_WEBHOOK_SECRET  the same secret string set in the Apps Script
  BRIEF_RECIPIENTS     "First Name:email, First Name:email"
                       e.g. "Shahrukh:shahrukh@worldofsmokenvape.com"
  DASHBOARD_URL        optional - defaults to the dev dashboard URL
  plus the same DATABASE_URL, GOOGLE_SERVICE_ACCOUNT_JSON,
  PACE_LOG_SHEET_ID, STORES_CONFIG_JSON the other services use.

RAILWAY SETUP:
  New service from this repo, Start Command: python morning_brief.py
  Cron Schedule (UTC): 0 11 * * *  -> 6:00 AM CDT (Mar-Nov)
                       0 12 * * *  -> 6:00 AM CST (Nov-Mar)
  "Run now" on the service sends a copy immediately (handy for testing).

LOCAL PREVIEW (no email sent):
  python morning_brief.py --dry-run          writes morning_brief_preview.html
"""
import argparse
import base64
import os
import smtplib
import sys
from datetime import timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

import pandas as pd

from dashboard_data import load_mama_snapshot
from lightspeed_client import load_config
from sales_pace import compute_pace, read_daily_log, store_local_today
from store_access import PACE_CALCULATOR_STORES_LIST, get_store_list
from store_regions import NORTH_STORES, SOUTH_STORES
from topshelf_invoices import read_log as read_tsn_log

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://wosv-dashboard-dev-production.up.railway.app").rstrip("/")
ERP_URL = "https://erp.topshelfnovelties.com"
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "email")
LOGOS = {  # cid -> (file, display width, display height)
    "wosv_logo": ("wosv_logo.png", 460, 100),
    "tsn_logo": ("tsn_logo.png", 460, 100),
    "mamas_logo": ("mamas_logo.png", 460, 100),
}

# Brand colors
WOSV = {"accent": "#1ab0e6", "text": "#0e8fc0", "border": "#b3e3f6", "tile": "#e8f7fd", "btn_bg": "#cdeefa", "btn_fg": "#0b6f96"}
TSN = {"accent": "#f8636b", "navy": "#154859", "border": "#c5d5dc", "tile": "#eef3f5"}
MAMAS = {"red": "#e0283a", "yellow": "#f9c455", "border": "#f3c3c7", "tile": "#fff4d6"}

SANS = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
SERIF = "Georgia, 'Times New Roman', serif"


# ---------------------------------------------------------------------------
# Gathering the numbers
# ---------------------------------------------------------------------------

def money0(v):
    return f"${v:,.0f}"


def money2(v):
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def gather():
    today = store_local_today()
    yesterday = today - timedelta(days=1)
    notes = []  # data problems worth flagging in the email itself

    # --- Retail ---
    config = load_config()
    stores = config["stores"]
    all_keys = list(stores.keys())
    listed = get_store_list(PACE_CALCULATOR_STORES_LIST, default_keys=all_keys)
    store_keys = [k for k in all_keys if k in listed]
    names = {k: stores[k].get("name", k) for k in store_keys}

    log = read_daily_log()
    log = log[log["store"].isin(store_keys)]
    latest = log["date"].max() if not log.empty else None
    if latest is None or latest < yesterday:
        notes.append(
            f"Store sales data only goes through {latest:%b %-d}." if latest is not None
            else "No store sales data found."
        )
    day = log[log["date"] == yesterday]
    paces = {k: compute_pace(log, k, today)["projected_monthly"] for k in store_keys if k in set(log["store"])}
    pace_by_name = {names[k]: v for k, v in paces.items()}
    top5 = (
        day.groupby("store")["total"].sum().sort_values(ascending=False).head(5)
        if not day.empty else pd.Series(dtype=float)
    )
    retail = {
        "sales": float(day["total"].sum()),
        "south_pace": sum(v for n, v in pace_by_name.items() if n in SOUTH_STORES),
        "north_pace": sum(v for n, v in pace_by_name.items() if n in NORTH_STORES),
        "top5": [(names.get(k, k), float(v), paces.get(k, 0.0)) for k, v in top5.items()],
    }

    # --- Wholesale ---
    tsn_log = read_tsn_log()
    month_start = yesterday.replace(day=1)
    mtd = tsn_log[(tsn_log["Date"] >= month_start) & (tsn_log["Date"] <= yesterday)]
    tsn_day = tsn_log[tsn_log["Date"] == yesterday].sort_values("Invoice #")
    mtd_sales = float(mtd["Product Sales"].sum())
    mtd_profit = float(mtd["Profit"].sum())
    day_sales = float(tsn_day["Product Sales"].sum())
    day_profit = float(tsn_day["Profit"].sum())
    tsn = {
        "revenue": float(mtd["Amount"].sum()),
        "profit": mtd_profit,
        "margin": (mtd_profit / mtd_sales * 100) if mtd_sales > 0 else None,
        "count": int(len(mtd)),
        "rows": tsn_day.to_dict("records"),
        "tot_amount": float(tsn_day["Amount"].sum()),
        "tot_shipping": float(tsn_day["Shipping"].sum()),
        "tot_profit": day_profit,
        "tot_margin": (day_profit / day_sales * 100) if day_sales > 0 else None,
        "watch": [r for r in tsn_day.to_dict("records") if r["Profit"] < 0],
        "missing_labels": [r for r in tsn_day.to_dict("records")
                           if r.get("Shipping Source") in ("Not found in ShipStation", "ShipStation not connected")],
    }

    # --- Manufacturing ---
    m_date, m_total, m_qty, m_failed, _ = load_mama_snapshot()
    if m_date != yesterday:
        notes.append("Mama's Sold wasn't updated for yesterday." if m_date is None
                     else f"Mama's Sold is from {m_date:%b %-d}, not yesterday.")
    mamas = {
        "units": m_qty or 0,
        "sales": m_total or 0.0,
        "store_count": sum(1 for v in stores.values() if v.get("refresh_token")) - len(m_failed or []),
    }

    return {"today": today, "yesterday": yesterday, "retail": retail, "tsn": tsn, "mamas": mamas, "notes": notes}


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _card_header(num, label, label_color, bar_color, logo_cid, subtitle):
    _, w, h = LOGOS[logo_cid]
    return f'''
      <tr><td style="padding:22px 22px 6px;">
        <table role="presentation" cellpadding="0" cellspacing="0"><tr>
          <td valign="top" style="width:6px; background:{bar_color}; border-radius:3px;">&nbsp;</td>
          <td style="padding-left:14px;">
            <div style="font-size:12px; letter-spacing:1.5px; font-weight:700;"><span style="color:#6b7280;">{num}</span><span style="color:#cbd0da; padding:0 10px;">|</span><span style="color:{label_color};">{label}</span></div>
            <div style="margin-top:10px;"><img src="cid:{logo_cid}" width="{w}" height="{h}" alt="" style="display:block; border:0; width:{w}px; height:{h}px;"></div>
            <div style="font-size:14px; color:#6b7280; margin-top:6px;">{subtitle}</div>
          </td>
        </tr></table>
      </td></tr>'''


def _tiles(tiles, bg, extra_style=""):
    """tiles: list of (label, value, sub, sub_color)."""
    cells = []
    for i, (label, value, sub, sub_color) in enumerate(tiles):
        if i:
            cells.append('<td width="8"></td>')
        sub_html = f'<div style="font-size:12px; color:{sub_color}; font-weight:600;">{sub}</div>' if sub else ""
        cells.append(
            f'<td width="{100 // len(tiles)}%" style="background:{bg}; border-radius:8px; padding:12px 14px;{extra_style}">'
            f'<div style="font-size:11px; letter-spacing:1.5px; color:#4b5563; font-weight:700;">{label}</div>'
            f'<div style="font-size:22px; font-weight:800; margin-top:4px;">{value}</div>{sub_html}</td>'
        )
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{"".join(cells)}</tr></table>'


def _button(href, text, bg, fg):
    return (f'<a href="{href}" style="display:block; background:{bg}; color:{fg}; text-align:center; '
            f'text-decoration:none; font-size:15px; font-weight:700; padding:12px; border-radius:8px;">{text} &rarr;</a>')


TH = 'style="padding:8px 4px; border-bottom:1px solid #d1d5db;"'
THR = 'align="right" style="padding:8px 4px; border-bottom:1px solid #d1d5db;"'


def _td(v, right=False, color=None, last=False, bold=False):
    border = "#d1d5db" if last else "#eceef2"
    style = f"padding:9px 4px; border-bottom:1px solid {border};"
    if color:
        style += f" color:{color};"
    if bold:
        style += " font-weight:800; border-bottom:0;"
    align = ' align="right"' if right else ""
    return f'<td{align} style="{style}">{v}</td>'


def build_html(d, first_name):
    r, t, m = d["retail"], d["tsn"], d["mamas"]
    yday = d["yesterday"].strftime("%b %-d").upper()

    # --- Retail rows ---
    if r["top5"]:
        rows = "".join(
            "<tr>" + _td(escape(n), last=i == len(r["top5"]) - 1)
            + _td(money0(s), True, last=i == len(r["top5"]) - 1)
            + _td(money0(p), True, last=i == len(r["top5"]) - 1) + "</tr>"
            for i, (n, s, p) in enumerate(r["top5"])
        )
    else:
        rows = f'<tr><td colspan="3" style="padding:12px 4px; color:#6b7280;">No store sales logged for yesterday.</td></tr>'

    retail = f'''
  <tr><td style="padding:30px 24px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {WOSV["border"]}; border-radius:12px;">
      {_card_header("01", "RETAIL", WOSV["text"], WOSV["accent"], "wosv_logo", "Yesterday")}
      <tr><td style="padding:14px 22px 0;">{_tiles([
          ("SALES", money0(r["sales"]), "All stores", WOSV["text"]),
          ("SOUTH PACE", money0(r["south_pace"]), "South stores", WOSV["text"]),
          ("NORTH PACE", money0(r["north_pace"]), "North stores", WOSV["text"]),
      ], WOSV["tile"])}</td></tr>
      <tr><td style="padding:20px 22px 0;">
        <div style="font-size:11px; letter-spacing:1.5px; color:#6b7280; font-weight:700; margin-bottom:4px;">TOP 5 STORES &bull; {yday}</div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse; font-size:13px;">
          <tr style="font-size:10px; letter-spacing:1px; color:#6b7280;"><td {TH}>LOCATION</td><td {THR}>YESTERDAY</td><td {THR}>MONTHLY PACE</td></tr>
          {rows}
        </table>
      </td></tr>
      <tr><td style="padding:18px 22px 22px;">{_button(DASHBOARD_URL + "/pace_calculator_page", "Open Pace Calculator", WOSV["btn_bg"], WOSV["btn_fg"])}</td></tr>
    </table>
  </td></tr>'''

    # --- Wholesale rows ---
    if t["rows"]:
        inv_rows = ""
        for x in t["rows"]:
            neg = x["Profit"] < 0
            margin = "&mdash;" if pd.isna(x["Margin %"]) else f'{x["Margin %"]:.1f}%'
            bg = ' style="background:#fff7ed;"' if neg else ""
            inv_rows += (f"<tr{bg}>" + _td(int(x["Invoice #"]), color="#6b7280") + _td(escape(str(x["Customer"])))
                         + _td(money2(x["Amount"]), True) + _td(money2(x["Shipping"]), True)
                         + _td(money2(x["Profit"]), True, color="#dc2626" if neg else None)
                         + _td(margin, True, color="#9ca3af" if margin == "&mdash;" else TSN["navy"]) + "</tr>")
        tm = "&mdash;" if t["tot_margin"] is None else f'{t["tot_margin"]:.1f}%'
        inv_rows += ("<tr>" + _td("", bold=True) + _td("Total", bold=True) + _td(money2(t["tot_amount"]), True, bold=True)
                     + _td(money2(t["tot_shipping"]), True, bold=True) + _td(money2(t["tot_profit"]), True, bold=True)
                     + _td(tm, True, bold=True) + "</tr>")
        invoice_table = f'''
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse; font-size:13px;">
          <tr style="font-size:10px; letter-spacing:1px; color:#6b7280;"><td {TH}>INVOICE</td><td {TH}>CUSTOMER</td><td {THR}>AMOUNT</td><td {THR}>SHIPPING</td><td {THR}>PROFIT</td><td {THR}>MARGIN</td></tr>
          {inv_rows}
        </table>'''
    else:
        invoice_table = '<div style="font-size:13px; color:#6b7280; padding:8px 0;">No invoices yesterday.</div>'

    watch_lines = [
        f'Invoice {int(x["Invoice #"])} ({escape(str(x["Customer"]))}) lost {money2(abs(x["Profit"]))}.' for x in t["watch"]
    ] + [
        f'Invoice {int(x["Invoice #"])} has no shipping label cost yet &mdash; shipping counted as $0.' for x in t["missing_labels"]
    ]
    watch = "".join(
        f'<div style="font-size:13px; margin-top:10px;"><span style="display:inline-block; width:10px; height:10px; '
        f'border-radius:5px; background:{TSN["accent"]}; margin-right:6px;"></span><strong>Watch:</strong> '
        f'<span style="color:#4b5563;">{w}</span></div>' for w in watch_lines
    )
    margin_sub = f'{t["margin"]:.1f}% margin' if t["margin"] is not None else ""

    wholesale = f'''
  <tr><td style="padding:22px 24px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {TSN["border"]}; border-radius:12px;">
      {_card_header("02", "WHOLESALE", TSN["navy"], TSN["accent"], "tsn_logo", "Month to date")}
      <tr><td style="padding:14px 22px 0;">{_tiles([
          ("REVENUE", money0(t["revenue"]), "", None),
          ("PROFIT", money0(t["profit"]), margin_sub, TSN["navy"]),
          ("INVOICES", f'{t["count"]:,}', "", None),
      ], TSN["tile"])}</td></tr>
      <tr><td style="padding:20px 22px 0;">
        <div style="font-size:11px; letter-spacing:1.5px; color:#6b7280; font-weight:700; margin-bottom:4px;">YESTERDAY&rsquo;S INVOICES &bull; {yday}</div>
        {invoice_table}
        {watch}
      </td></tr>
      <tr><td style="padding:18px 22px 22px;">{_button(ERP_URL, "View ERP", TSN["navy"], "#ffffff")}</td></tr>
    </table>
  </td></tr>'''

    manufacturing = f'''
  <tr><td style="padding:22px 24px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {MAMAS["border"]}; border-radius:12px;">
      {_card_header("03", "MANUFACTURING", MAMAS["red"], MAMAS["red"], "mamas_logo", f'Sold yesterday, across all {m["store_count"]} stores')}
      <tr><td style="padding:14px 22px 22px;">{_tiles([
          ("UNITS", f'{m["units"]:,.0f}', "", None),
          ("SALES", money2(m["sales"]), "", None),
      ], MAMAS["tile"], f' border-top:3px solid {MAMAS["yellow"]};')}</td></tr>
    </table>
  </td></tr>'''

    notes = ""
    if d["notes"]:
        notes = (f'<tr><td style="padding:18px 32px 0;"><div style="background:#fef3c7; border-radius:8px; padding:10px 14px; '
                 f'font-size:13px; color:#92400e;"><strong>Heads up:</strong> {" ".join(escape(n) for n in d["notes"])} '
                 f'The nightly refresh may not have finished.</div></td></tr>')

    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Morning Brief</title></head>
<body style="margin:0; padding:0; background:#f7f6f2;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7f6f2;">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%; max-width:600px; background:#ffffff; font-family:{SANS}; color:#111827;">
  <tr><td style="padding:28px 32px 0;">
    <div style="font-family:{SERIF}; font-size:13px; letter-spacing:3px; color:#111827; text-transform:uppercase; padding-bottom:14px; border-bottom:1px solid #cbd0da;">
      <span style="display:inline-block; background:{WOSV["accent"]}; color:#ffffff; font-size:14px; letter-spacing:0; padding:3px 8px; margin-right:10px;">W</span>The Morning Brief
    </div>
  </td></tr>
  <tr><td style="padding:26px 32px 0;">
    <div style="font-family:{SERIF}; font-size:40px; line-height:1.1; font-weight:700; color:#0b0f19;">Good Morning, {escape(first_name)}</div>
    <div style="font-family:{SERIF}; font-size:18px; color:#111827; margin-top:12px;">{d["today"]:%A, %B %-d, %Y}</div>
  </td></tr>
  {notes}
  {retail}
  {wholesale}
  {manufacturing}
  <tr><td style="padding:32px 32px 28px;">
    <div style="border-top:1px solid #cbd0da; padding-top:14px; font-size:11px; color:#6b7280;">Reporting period: 12:00 AM&ndash;11:59 PM CT</div>
  </td></tr>
</table>
</td></tr></table>
</body></html>'''


def build_text(d):
    """Plain-text fallback for mail apps that don't show HTML."""
    r, t, m = d["retail"], d["tsn"], d["mamas"]
    lines = [f"The Morning Brief - {d['today']:%A, %B %-d, %Y}", ""]
    lines += [f"RETAIL: yesterday {money0(r['sales'])} | South pace {money0(r['south_pace'])} | North pace {money0(r['north_pace'])}"]
    lines += [f"  {n}: {money0(s)} (pace {money0(p)})" for n, s, p in r["top5"]]
    lines += ["", f"WHOLESALE MTD: revenue {money0(t['revenue'])} | profit {money0(t['profit'])} | {t['count']} invoices"]
    lines += [f"  #{int(x['Invoice #'])} {x['Customer']}: {money2(x['Amount'])}, profit {money2(x['Profit'])}" for x in t["rows"]]
    lines += ["", f"MAMA'S SOLD yesterday: {m['units']:,.0f} units, {money2(m['sales'])}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def parse_recipients(raw):
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, email = part.rpartition(":")
        email = email.strip()
        out.append((name.strip() or email.split("@")[0].title(), email))
    return out


def send_via_webhook(d, first_name, to_email):
    """Hands the finished email to the Apps Script web app, which sends it
    from Gmail. Logos go along base64-encoded and are attached inline there."""
    import requests
    images = {}
    for cid, (filename, _, _) in LOGOS.items():
        with open(os.path.join(ASSETS, filename), "rb") as f:
            images[cid] = base64.b64encode(f.read()).decode()
    resp = requests.post(
        os.environ["MAIL_WEBHOOK_URL"],
        json={
            "secret": os.getenv("MAIL_WEBHOOK_SECRET", ""),
            "to": to_email,
            "subject": f"Morning Brief \u2014 {d['today']:%a, %b %-d}",
            "html": build_html(d, first_name),
            "text": build_text(d),
            "name": "The Morning Brief",
            "images": images,
        },
        timeout=60,
    )
    try:
        result = resp.json()
    except ValueError:
        raise RuntimeError(
            f"Mail web app returned HTTP {resp.status_code} without JSON - check that it's deployed "
            "with access set to 'Anyone' and that MAIL_WEBHOOK_URL is the /exec URL."
        )
    if not result.get("ok"):
        raise RuntimeError(f"Mail web app refused to send: {result.get('error')}")


def build_message(d, first_name, to_email, sender):
    msg = MIMEMultipart("related")
    msg["Subject"] = f"Morning Brief — {d['today']:%a, %b %-d}"
    msg["From"] = formataddr(("The Morning Brief", sender))
    msg["To"] = to_email
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(build_text(d), "plain", "utf-8"))
    alt.attach(MIMEText(build_html(d, first_name), "html", "utf-8"))
    msg.attach(alt)
    for cid, (filename, _, _) in LOGOS.items():
        with open(os.path.join(ASSETS, filename), "rb") as f:
            img = MIMEImage(f.read(), _subtype="png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=filename)
        msg.attach(img)
    return msg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="write morning_brief_preview.html instead of sending")
    parser.add_argument("--to", help='override recipients, same format as BRIEF_RECIPIENTS')
    args = parser.parse_args()

    d = gather()
    recipients = parse_recipients(args.to or os.getenv("BRIEF_RECIPIENTS"))

    if args.dry_run:
        html = build_html(d, recipients[0][0] if recipients else "there")
        for cid, (filename, _, _) in LOGOS.items():
            html = html.replace(f"cid:{cid}", os.path.join(ASSETS, filename))
        with open("morning_brief_preview.html", "w") as f:
            f.write(html)
        print("Wrote morning_brief_preview.html")
        return

    if not recipients:
        raise RuntimeError("BRIEF_RECIPIENTS is empty - nobody to send to.")

    if os.getenv("MAIL_WEBHOOK_URL"):
        for first_name, email in recipients:
            send_via_webhook(d, first_name, email)
            print(f"Sent to {email}")
        print(f"Morning brief sent to {len(recipients)} recipient(s).")
        return

    sender = os.getenv("GMAIL_ADDRESS")
    password = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not (sender and password):
        raise RuntimeError("Set MAIL_WEBHOOK_URL (Apps Script) - or GMAIL_ADDRESS + GMAIL_APP_PASSWORD on Railway Pro.")

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(sender, password)
        for first_name, email in recipients:
            smtp.send_message(build_message(d, first_name, email, sender))
            print(f"Sent to {email}")
    print(f"Morning brief sent to {len(recipients)} recipient(s).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Morning brief FAILED: {e}")
        sys.exit(1)

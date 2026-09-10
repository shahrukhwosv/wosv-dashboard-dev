"""
Monthly Reports page (monthly_reports_page.py).

Pick a store and a month, click Generate, download a zip with all six
reports for that store/month:
  - Inventory_Valuation.csv
  - Item_Summary.csv
  - Tax_Category_Summary.pdf
  - Z_Out_Store_Close.pdf      (Payments)
  - Receiving_Voucher_Detail.pdf   (Purchase Orders)
  - Payout_List.pdf            (Adds / Payouts - see reports.py, lowest
    confidence of the six)

Respects per-user store access AND the admin-editable Standard Stores
list (see store_access.py) - same restriction as Commissions,
Transactions, and Touch Tell. Category Sales is the only page that isn't
narrowed to that list.

Streams the zip straight to the browser rather than writing to disk -
Railway's filesystem is ephemeral, so nothing would survive between
requests anyway.

Before trusting these for real bookkeeping: generate one for a month
you can already see in Lightspeed and diff the totals against what
Lightspeed shows you directly. See the confidence notes at the top of
reports.py - a couple of the numbers (discount handling, and especially
Adds/Payouts) are reconstructed from field names that haven't been
checked against a live account yet.
"""
import csv
import io
import zipfile
from calendar import monthrange
from datetime import date

import streamlit as st

import lightspeed_client as ls
import lookups as lookups_mod
import reports
from pdf_builder import build_pdf_report
from store_access import get_page_store_keys, STANDARD_STORES_LIST, DEFAULT_STANDARD_STORE_KEYS

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

st.title("Monthly Reports")

config = ls.load_config()
standard_keys = get_page_store_keys(
    config, list_name=STANDARD_STORES_LIST, default_keys=DEFAULT_STANDARD_STORE_KEYS
)
store_keys = sorted(
    standard_keys,
    key=lambda key: (config["stores"][key].get("name") or key).casefold(),
)
store_names = {key: config["stores"][key].get("name", key) for key in store_keys}

if not store_keys:
    st.info('No stores available. Check the Standard Stores list on the Manage Users page.')
    st.stop()

col1, col2, col3 = st.columns(3)
with col1:
    selected_store_key = st.selectbox(
        "Store", store_keys, format_func=lambda key: store_names[key]
    )
with col2:
    today = date.today()
    selected_month_name = st.selectbox("Month", MONTH_NAMES, index=today.month - 2 if today.month > 1 else 11)
    selected_month = MONTH_NAMES.index(selected_month_name) + 1
with col3:
    year_options = list(range(2024, today.year + 1))
    selected_year = st.selectbox("Year", year_options, index=len(year_options) - 1)

start = date(selected_year, selected_month, 1)
end = date(selected_year, selected_month, monthrange(selected_year, selected_month)[1])
month_label = start.strftime("%Y-%m")
store_name = store_names[selected_store_key]


def _write_csv(headers, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue()


def _write_pdf(title, subtitle, headers, rows, totals_row=None, col_widths=None):
    buf = io.BytesIO()
    build_pdf_report(buf, title, subtitle, headers, rows, totals_row, col_widths)
    return buf.getvalue()


def generate_reports_zip(config, store_key, store_name, start, end, month_label):
    subtitle = f"{store_name} -- {start.isoformat()} to {end.isoformat()}"
    zip_buf = io.BytesIO()

    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        st.write("Loading reference data (categories, vendors, tax categories, payment types)...")
        lookups = lookups_mod.build_lookups(config, store_key)

        st.write("Pulling completed sales for the month (this is the slow part)...")
        sales = reports.fetch_completed_sales(config, store_key, start, end)
        st.write(f"{len(sales)} completed sales found.")

        st.write("Building Inventory Valuation...")
        headers, rows, _ = reports.build_inventory_valuation(config, store_key, lookups, store_name)
        zf.writestr(f"Inventory_Valuation_{month_label}.csv", _write_csv(headers, rows))

        st.write("Building Item Summary...")
        headers, rows, _ = reports.build_item_summary(config, store_key, lookups, sales)
        zf.writestr(f"Item_Summary_{month_label}.csv", _write_csv(headers, rows))

        st.write("Building Tax Category Summary...")
        headers, rows, totals = reports.build_sales_tax_summary(lookups, sales, store_name, start, end)
        zf.writestr(
            f"Tax_Category_Summary_{month_label}.pdf",
            _write_pdf("Sales Tax", subtitle, headers, rows, totals),
        )

        st.write("Building Payments (Z-Out / Store Close)...")
        headers, rows, totals, summary_lines = reports.build_payments_report(lookups, sales)
        zf.writestr(
            f"Z_Out_Store_Close_{month_label}.pdf",
            _write_pdf("Payments", subtitle + "\n" + " | ".join(summary_lines), headers, rows, totals),
        )

        st.write("Building Receiving Voucher Detail (Purchase Orders)...")
        headers, rows, totals = reports.build_purchase_orders_report(config, store_key, lookups, start, end)
        zf.writestr(
            f"Receiving_Voucher_Detail_{month_label}.pdf",
            _write_pdf(
                "Purchase Orders", subtitle, headers, rows, totals,
                col_widths=[0.6, 0.8, 1.0, 1.6, 0.9, 0.9, 0.8, 0.8, 0.9],
            ),
        )

        st.write("Building Adds / Payouts...")
        headers, rows, totals = reports.build_adds_payouts_report(config, store_key, lookups, start, end)
        zf.writestr(
            f"Payout_List_{month_label}.pdf",
            _write_pdf("Adds / Payouts", subtitle, headers, rows, totals),
        )

    zip_buf.seek(0)
    return zip_buf.getvalue()


if st.button("Generate reports", type="primary"):
    with st.spinner(f"Generating reports for {store_name}, {selected_month_name} {selected_year}..."):
        zip_bytes = generate_reports_zip(config, selected_store_key, store_name, start, end, month_label)

    st.success("Done.")
    st.download_button(
        label="Download reports (zip)",
        data=zip_bytes,
        file_name=f"{store_name.replace(' ', '_')}_Reports_{month_label}.zip",
        mime="application/zip",
    )

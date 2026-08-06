"""
Touch Tell Invoice Matching

Streamlit "page" routed by the top-level app.py, same pattern as
reconciliation_page.py.

Pulls unresolved (blank status) rows from the Touch Tell Google Sheet,
checks each against that store's Lightspeed purchase orders, and:
  - marks matches "Ready to Pay" directly in the sheet
  - offers a downloadable PDF grouped by store with totals
  - flags anything that doesn't match, with a reason
"""
import json

import streamlit as st

from lightspeed_client import load_config
from lightspeed_po import fetch_purchase_orders_for_vendor
from sheets_client import read_invoice_rows, write_ready_to_pay, write_paid
from touch_tell_matching import match_store_invoices
from touch_tell_pdf import build_ready_to_pay_pdf

st.title("Touch Tell Invoice Matching")
st.caption(
    "Checks unresolved Touch Tell invoices against each store's Lightspeed "
    "purchase orders (stage must be Ordered or later, amount must match) "
    "and marks the ones that are ready to pay."
)

with open("touch_tell_config.json") as f:
    tt_config = json.load(f)

sheet_id = tt_config["google_sheet_id"]
worksheet_name = tt_config["worksheet_name"]
vendor_name = tt_config["vendor_name"]
store_sheet_to_key = tt_config["store_sheet_to_key"]

if sheet_id == "PASTE_YOUR_SHEET_ID_HERE":
    st.error("touch_tell_config.json still has a placeholder google_sheet_id. Add your sheet's ID first.")
    st.stop()

ls_config = load_config()
connected_store_keys = {
    key for key, val in ls_config["stores"].items() if val.get("refresh_token")
}

store_choice = st.selectbox(
    "Which store(s) to check?",
    ["All stores"] + sorted(store_sheet_to_key.keys()),
)

dry_run = st.checkbox(
    "Dry run (check everything, but don't write to the sheet or mark anything Ready to Pay)",
    value=True,
)
run = st.button("Check Touch Tell invoices", type="primary")

if run:
    with st.spinner("Reading the invoice sheet..."):
        try:
            all_rows = read_invoice_rows(sheet_id, worksheet_name, store_sheet_to_key)
        except Exception as e:
            st.error(f"Could not read the Google Sheet: {e}")
            st.stop()

    unresolved_rows = [r for r in all_rows if r["status"] is None]

    if store_choice != "All stores":
        unresolved_rows = [r for r in unresolved_rows if r["store_sheet_name"] == store_choice]

    if not unresolved_rows:
        st.session_state.pop("tt_results", None)
        st.success("No unresolved invoices - every row already has a status.")
        st.stop()

    # Group unresolved rows by store
    rows_by_store = {}
    for row in unresolved_rows:
        rows_by_store.setdefault(row["store_sheet_name"], []).append(row)

    ready_by_store = {}
    all_exceptions = []
    cells_to_write = []

    for store_name, rows in rows_by_store.items():
        store_key = store_sheet_to_key.get(store_name)

        if not store_key:
            for row in rows:
                all_exceptions.append((row, f"'{store_name}' isn't in touch_tell_config.json's store mapping."))
            continue

        if store_key not in connected_store_keys:
            for row in rows:
                all_exceptions.append((row, f"'{store_name}' ({store_key}) isn't connected to Lightspeed yet."))
            continue

        with st.spinner(f"Pulling {store_name}'s purchase orders..."):
            try:
                pos = fetch_purchase_orders_for_vendor(ls_config, store_key, vendor_name)
            except Exception as e:
                for row in rows:
                    all_exceptions.append((row, f"Error pulling Lightspeed data for {store_name}: {e}"))
                continue

        ready, exceptions = match_store_invoices(rows, pos)
        if ready:
            ready_by_store[store_name] = ready
            for row in ready:
                cells_to_write.append((row["row"], row["status_col"]))
        all_exceptions.extend(exceptions)

    wrote_to_sheet = False
    write_error = None
    if ready_by_store and not dry_run:
        with st.spinner("Writing 'Ready to Pay' back to the sheet..."):
            try:
                write_ready_to_pay(sheet_id, worksheet_name, cells_to_write)
                wrote_to_sheet = True
            except Exception as e:
                write_error = str(e)

    # Save everything needed to render results, so later interactions
    # (like clicking the PDF button) don't lose this on the next rerun.
    st.session_state["tt_results"] = {
        "ready_by_store": ready_by_store,
        "all_exceptions": all_exceptions,
        "dry_run": dry_run,
        "wrote_to_sheet": wrote_to_sheet,
        "write_error": write_error,
    }

# Render results (from session_state, so this survives reruns triggered by
# the PDF download button or anything else on the page).
if "tt_results" in st.session_state:
    results = st.session_state["tt_results"]
    ready_by_store = results["ready_by_store"]
    all_exceptions = results["all_exceptions"]
    total_ready = sum(len(v) for v in ready_by_store.values())

    st.subheader("Results")
    c1, c2 = st.columns(2)
    c1.metric("✅ Ready to pay", total_ready)
    c2.metric("⚠️ Exceptions", len(all_exceptions))

    if ready_by_store:
        if results["dry_run"]:
            st.info(f"Dry run: found {total_ready} invoice(s) that WOULD be marked Ready to Pay. Nothing was written to the sheet.")
        elif results["wrote_to_sheet"]:
            st.success(f"Marked {total_ready} invoice(s) as Ready to Pay in the sheet.")
        elif results["write_error"]:
            st.error(f"Matched {total_ready} invoice(s) but couldn't write back to the sheet: {results['write_error']}")

        st.markdown("### ✅ Ready to pay")
        for store_name, rows in ready_by_store.items():
            store_total = sum(r["amount"] for r in rows)
            with st.expander(f"{store_name} - {len(rows)} invoice(s), ${store_total:,.2f}"):
                for r in rows:
                    st.write(f"Invoice {r['invoice_number']}: ${r['amount']:,.2f}")

    if all_exceptions:
        st.markdown("### ⚠️ Exceptions")
        for row, reason in all_exceptions:
            st.write(f"**{row['store_sheet_name']}** — Invoice {row['invoice_number']} (${row['amount'] if row['amount'] is not None else '?'}): {reason}")

st.divider()
st.subheader("Ready to Pay PDF")
st.caption(
    "Pulls every invoice currently marked 'Ready to Pay' in the sheet - "
    "across all stores, including ones you set manually - and builds a PDF "
    "grouped by store with totals. Independent of the check above."
)

if st.button("Get PDF"):
    with st.spinner("Reading the invoice sheet..."):
        try:
            all_rows = read_invoice_rows(sheet_id, worksheet_name, store_sheet_to_key)
        except Exception as e:
            st.error(f"Could not read the Google Sheet: {e}")
            st.stop()

    ready_rows = [r for r in all_rows if r["status"] == "Ready to Pay"]
    bad_amount_rows = [r for r in ready_rows if r["amount"] is None]
    ready_rows = [r for r in ready_rows if r["amount"] is not None]

    if bad_amount_rows:
        st.warning(
            f"{len(bad_amount_rows)} 'Ready to Pay' row(s) have an amount that "
            f"couldn't be read and were left out of the PDF: " +
            ", ".join(f"{r['store_sheet_name']} #{r['invoice_number']}" for r in bad_amount_rows)
        )

    if not ready_rows:
        st.session_state.pop("tt_pdf_ready_by_store", None)
        st.info("No invoices are currently marked 'Ready to Pay' in the sheet.")
    else:
        ready_by_store_all = {}
        for row in ready_rows:
            ready_by_store_all.setdefault(row["store_sheet_name"], []).append(row)
        st.session_state["tt_pdf_ready_by_store"] = ready_by_store_all

if "tt_pdf_ready_by_store" in st.session_state:
    ready_by_store_all = st.session_state["tt_pdf_ready_by_store"]
    total_count = sum(len(v) for v in ready_by_store_all.values())
    grand_total = sum(r["amount"] for rows in ready_by_store_all.values() for r in rows if r["amount"] is not None)
    st.write(f"{total_count} invoice(s) across {len(ready_by_store_all)} store(s), ${grand_total:,.2f} total.")

    pdf_buffer = build_ready_to_pay_pdf(ready_by_store_all)
    st.download_button(
        label="Download PDF",
        data=pdf_buffer,
        file_name="touch_tell_ready_to_pay.pdf",
        mime="application/pdf",
    )

    st.markdown("#### After you've paid Touch Tell")
    st.caption(
        "Marks every invoice in the PDF above as 'Paid' in the sheet, so "
        "they won't show up as Ready to Pay next time. This is separate "
        "from downloading the PDF - click it once payment is actually done."
    )
    if st.button("Mark these as Paid", type="primary"):
        st.session_state["tt_paid_confirm_pending"] = True

    if st.session_state.get("tt_paid_confirm_pending"):
        confirm_value = st.text_input(
            "Type 'paid' to confirm you've actually paid Touch Tell:",
            key="tt_paid_confirm_input",
        )
        confirm_clicked = st.button("Confirm")
        if confirm_clicked:
            if confirm_value.strip().lower() == "paid":
                cells_to_update = [
                    (row["row"], row["status_col"])
                    for rows in ready_by_store_all.values()
                    for row in rows
                ]
                try:
                    write_paid(sheet_id, worksheet_name, cells_to_update)
                    st.success(f"Marked {total_count} invoice(s) as Paid in the sheet.")
                    st.session_state.pop("tt_pdf_ready_by_store", None)
                    st.session_state.pop("tt_paid_confirm_pending", None)
                except Exception as e:
                    st.error(f"Could not update the sheet: {e}")
            else:
                st.error("That didn't match 'paid' - nothing was changed.")

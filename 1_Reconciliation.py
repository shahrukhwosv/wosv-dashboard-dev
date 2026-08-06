"""
Card Payment Reconciliation Tool

This is a Streamlit "page" - part of the same app as app.py (the commission
report). Streamlit auto-detects anything in the pages/ folder and adds it
to the sidebar navigation automatically. Run the whole app the same way as
before: streamlit run app.py (from the project root).
"""

import sys
import os
# Make sure root-level modules (lightspeed_client.py, valor_parser.py, etc.)
# are importable from this pages/ subfolder, regardless of how Streamlit
# sets up sys.path in a given deployment environment.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import io
from datetime import date, timedelta

import streamlit as st
import pandas as pd

from lightspeed_client import load_config, fetch_employees
from reconciliation_lightspeed import fetch_card_sales
from valor_parser import parse_valor_csv
from reconciliation_engine import reconcile

st.set_page_config(page_title="Card Payment Reconciliation", layout="wide")
st.title("Card Payment Reconciliation")
st.caption(
    "Finds Lightspeed sales where the card charge is missing or doesn't "
    "match — usually a sign the amount was mistyped or never charged."
)

config = load_config()
stores = config["stores"]

connected_stores = {
    key: val for key, val in stores.items() if val.get("refresh_token")
}

if not connected_stores:
    st.error("No stores are connected yet. See README.md to connect a store first.")
    st.stop()

col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("1. Choose store & date range")
    store_label_to_key = {v["name"]: k for k, v in connected_stores.items()}
    selected_label = st.selectbox("Store", list(store_label_to_key.keys()))
    store_key = store_label_to_key[selected_label]

    default_start = date.today() - timedelta(days=1)
    start_date = st.date_input("Period start", value=default_start)
    end_date = st.date_input("Period end", value=date.today())

with col2:
    st.subheader("2. Upload ValorPay report")
    uploaded_file = st.file_uploader(
        f"Upload the ValorPay batch report CSV for {selected_label}", type=["csv"]
    )

run = st.button("Run Reconciliation", type="primary", disabled=(uploaded_file is None))

if run:
    if start_date > end_date:
        st.error("Period start must be before period end.")
        st.stop()

    with st.spinner("Pulling Lightspeed sales..."):
        try:
            employees = fetch_employees(config, store_key)
            card_sales, cash_count, unknown_count = fetch_card_sales(
                config, store_key, start_date, end_date, employees
            )
        except Exception as e:
            st.error(f"Could not pull Lightspeed data: {e}")
            st.stop()

    valor_sales, other_valor_tx = parse_valor_csv(uploaded_file)

    st.info(
        f"Lightspeed: {len(card_sales)} card sale(s) found "
        f"({cash_count} cash sale(s) excluded, {unknown_count} sale(s) with "
        f"undetermined payment type excluded — see note below). "
        f"ValorPay: {len(valor_sales)} charge(s) in the uploaded file."
    )
    if unknown_count > 0:
        st.warning(
            f"{unknown_count} Lightspeed sale(s) had a payment type we "
            f"couldn't confidently identify, so they were left out of "
            f"matching entirely (not counted as missing charges). Run "
            f"`python inspect_payment_types.py {store_key}` if this number "
            f"seems high, and share the output so we can fix the detection."
        )

    results = reconcile(card_sales, valor_sales)

    st.subheader("Results")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("✅ Matched", len(results["matched"]))
    m2.metric("⚠️ Likely mismatch", len(results["likely_mismatch"]))
    m3.metric("❌ Missing charge", len(results["missing_charge"]))
    m4.metric("❓ Unexplained Valor charge", len(results["unexplained_valor_charge"]))

    # --- Missing charge: Lightspeed sale with NO nearby Valor charge at all ---
    if results["missing_charge"]:
        st.markdown("### ❌ Missing charge (rung up in Lightspeed, no card charge found)")
        df = pd.DataFrame([
            {
                "Sale ID": r["sale_id"],
                "Employee": r["employee_name"],
                "Lightspeed Total": r["total"],
                "Time": r["timestamp"],
            }
            for r in results["missing_charge"]
        ]).sort_values("Time")
        st.dataframe(df, use_container_width=True)

    # --- Likely mismatch: amount typed wrong ---
    if results["likely_mismatch"]:
        st.markdown("### ⚠️ Likely mismatch (amount charged doesn't match the sale)")
        df = pd.DataFrame([
            {
                "Sale ID": r["lightspeed"]["sale_id"],
                "Employee": r["lightspeed"]["employee_name"],
                "Lightspeed Total": r["lightspeed"]["total"],
                "Valor Charge (base)": r["closest_valor"]["base_amount"],
                "Difference": r["amount_diff"],
                "Lightspeed Time": r["lightspeed"]["timestamp"],
                "Valor Time": r["closest_valor"]["timestamp"],
            }
            for r in results["likely_mismatch"]
        ]).sort_values("Lightspeed Time")
        st.dataframe(df, use_container_width=True)

    # --- Unexplained Valor charge: charge with no matching sale ---
    if results["unexplained_valor_charge"]:
        st.markdown("### ❓ Unexplained Valor charge (no matching Lightspeed sale)")
        df = pd.DataFrame([
            {
                "Valor Amount": r["base_amount"],
                "Card": f"{r['card_scheme']} {r['masked_card']}",
                "Time": r["timestamp"],
            }
            for r in results["unexplained_valor_charge"]
        ]).sort_values("Time")
        st.dataframe(df, use_container_width=True)

    with st.expander(f"✅ Show matched transactions ({len(results['matched'])})"):
        if results["matched"]:
            df = pd.DataFrame([
                {
                    "Sale ID": r["lightspeed"]["sale_id"],
                    "Employee": r["lightspeed"]["employee_name"],
                    "Amount": r["lightspeed"]["total"],
                    "Lightspeed Time": r["lightspeed"]["timestamp"],
                    "Valor Time": r["valor"]["timestamp"],
                }
                for r in results["matched"]
            ]).sort_values("Lightspeed Time")
            st.dataframe(df, use_container_width=True)

    # --- Excel export of everything ---
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame([
            {"Sale ID": r["sale_id"], "Employee": r["employee_name"], "Lightspeed Total": r["total"], "Time": r["timestamp"]}
            for r in results["missing_charge"]
        ]).to_excel(writer, index=False, sheet_name="Missing Charge")

        pd.DataFrame([
            {
                "Sale ID": r["lightspeed"]["sale_id"],
                "Employee": r["lightspeed"]["employee_name"],
                "Lightspeed Total": r["lightspeed"]["total"],
                "Valor Charge": r["closest_valor"]["base_amount"],
                "Difference": r["amount_diff"],
                "Lightspeed Time": r["lightspeed"]["timestamp"],
                "Valor Time": r["closest_valor"]["timestamp"],
            }
            for r in results["likely_mismatch"]
        ]).to_excel(writer, index=False, sheet_name="Likely Mismatch")

        pd.DataFrame([
            {"Valor Amount": r["base_amount"], "Card": f"{r['card_scheme']} {r['masked_card']}", "Time": r["timestamp"]}
            for r in results["unexplained_valor_charge"]
        ]).to_excel(writer, index=False, sheet_name="Unexplained Valor Charge")

        pd.DataFrame([
            {
                "Sale ID": r["lightspeed"]["sale_id"],
                "Employee": r["lightspeed"]["employee_name"],
                "Amount": r["lightspeed"]["total"],
                "Lightspeed Time": r["lightspeed"]["timestamp"],
                "Valor Time": r["valor"]["timestamp"],
            }
            for r in results["matched"]
        ]).to_excel(writer, index=False, sheet_name="Matched")
    buffer.seek(0)

    st.download_button(
        label="Download full report as Excel",
        data=buffer,
        file_name=f"reconciliation_{selected_label}_{start_date}_{end_date}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

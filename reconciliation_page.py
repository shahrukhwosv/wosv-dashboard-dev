"""
Card Payment Reconciliation Tool

This is a Streamlit "page" routed by the top-level app.py.

Pick a store and date range, upload that store's ValorPay batch report CSV,
and see exactly which Lightspeed sales are missing a matching card charge
or were charged the wrong amount.
"""

import io
from datetime import date, timedelta

import streamlit as st
import pandas as pd

from lightspeed_client import load_config, fetch_employees
from reconciliation_lightspeed import fetch_card_sales
from valor_parser import parse_valor_csv
from reconciliation_engine import reconcile

st.title("Card Payment Reconciliation")
st.caption(
    "Finds Lightspeed sales where the card charge is missing or doesn't "
    "match — usually a sign the amount was mistyped or never charged."
)

with st.expander("🔧 Timezone diagnostics (temporary - for debugging)"):
    import sys
    from datetime import datetime, timezone as tz_module
    from zoneinfo import ZoneInfo

    st.write(f"Python version: {sys.version}")

    try:
        import tzdata
        st.write(f"✅ tzdata package IS installed (version: {tzdata.IANA_VERSION})")
    except ImportError:
        st.write("❌ tzdata package is NOT installed / not importable")

    test_utc = datetime(2026, 7, 12, 15, 0, 0, tzinfo=tz_module.utc)
    st.write(f"Test UTC time: {test_utc}")
    try:
        chicago = test_utc.astimezone(ZoneInfo("America/Chicago"))
        st.write(f"→ Converted to America/Chicago: **{chicago}** (offset: {chicago.utcoffset()})")
        st.write("Expected in July: offset should be -1 day, 19:00:00 (i.e. UTC-5, Central Daylight Time)")
    except Exception as e:
        st.write(f"❌ Error converting to America/Chicago: {e}")

    try:
        newyork = test_utc.astimezone(ZoneInfo("America/New_York"))
        st.write(f"→ Converted to America/New_York: **{newyork}** (offset: {newyork.utcoffset()})")
    except Exception as e:
        st.write(f"❌ Error converting to America/New_York: {e}")

config = load_config()
stores = config["stores"]

connected_stores = {
    key: val for key, val in stores.items()
    if val.get("refresh_token") and not val.get("pace_only")
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
    st.subheader("2. Upload ValorPay report(s)")
    uploaded_files = st.file_uploader(
        f"Upload one or more ValorPay batch report CSVs for {selected_label} "
        f"(e.g. one per day, if your date range spans multiple batches)",
        type=["csv"],
        accept_multiple_files=True,
    )

run = st.button("Run Reconciliation", type="primary", disabled=(not uploaded_files))

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

    valor_sales = []
    other_valor_tx = []
    duplicate_tx_ids = set()
    seen_tx_ids = set()

    for f in uploaded_files:
        file_sales, file_others = parse_valor_csv(f)
        for s in file_sales:
            tx_id = s.get("transaction_id")
            if tx_id and tx_id in seen_tx_ids:
                duplicate_tx_ids.add(tx_id)
                continue  # skip - same charge already loaded from another file
            if tx_id:
                seen_tx_ids.add(tx_id)
            valor_sales.append(s)
        other_valor_tx.extend(file_others)

    if duplicate_tx_ids:
        st.warning(
            f"{len(duplicate_tx_ids)} transaction(s) appeared in more than one "
            f"uploaded file (same Transaction ID) - duplicates were skipped so "
            f"they aren't counted twice."
        )

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

    mismatch_net = sum(r["amount_diff"] for r in results["likely_mismatch"])
    missing_total = sum(r["total"] for r in results["missing_charge"])
    unexplained_total = sum(r["base_amount"] for r in results["unexplained_valor_charge"])
    overall_net = mismatch_net - missing_total + unexplained_total

    d1, d2, d3, d4 = st.columns(4)
    d1.metric(
        "Mismatch $ (Valor − LS)", f"${mismatch_net:,.2f}",
        help="Sum of the Difference column above. Positive = Valor charged more than Lightspeed rang up; negative = Valor charged less."
    )
    d2.metric(
        "Missing charge $", f"-${missing_total:,.2f}" if missing_total else "$0.00",
        help="Total Lightspeed sales with no card charge found at all - money never collected."
    )
    d3.metric(
        "Unexplained Valor $", f"+${unexplained_total:,.2f}" if unexplained_total else "$0.00",
        help="Total card charges with no matching Lightspeed sale - extra money collected with nothing rung up for it."
    )
    d4.metric(
        "Net difference (Valor − LS)", f"${overall_net:,.2f}",
        help="All three above combined. Positive = Valor collected more overall than Lightspeed's sales account for; negative = Valor collected less."
    )

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
                "Difference (Valor - Lightspeed)": r["amount_diff"],
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

    def _matched_row(r):
        base = {
            "Sale ID": r["lightspeed"]["sale_id"],
            "Employee": r["lightspeed"]["employee_name"],
            "Amount": r["lightspeed"]["total"],
            "Lightspeed Time": r["lightspeed"]["timestamp"],
            "Delayed": "Yes" if r["delayed"] else "",
        }
        if r["split"]:
            base["Valor Time"] = ", ".join(
                v["timestamp"].strftime("%H:%M") for v in r["valor_charges"]
            )
            base["Split Payment"] = " + ".join(
                f"${v['base_amount']:.2f}" for v in r["valor_charges"]
            )
        else:
            base["Valor Time"] = r["valor"]["timestamp"]
            base["Split Payment"] = ""
        return base

    with st.expander(f"✅ Show matched transactions ({len(results['matched'])})"):
        if results["matched"]:
            delayed_count = sum(1 for r in results["matched"] if r["delayed"])
            split_count = sum(1 for r in results["matched"] if r["split"])
            if delayed_count:
                st.caption(
                    f"{delayed_count} of these matched only after a longer-than-"
                    f"usual delay (more than {2} minutes between the Lightspeed "
                    f"sale and the Valor charge) - amount was still exactly "
                    f"right, just entered late. Marked below."
                )
            if split_count:
                st.caption(
                    f"{split_count} of these were paid across two or more cards "
                    f"(split tender) - the Valor charges add up exactly to the "
                    f"sale total. Marked below."
                )
            df = pd.DataFrame([_matched_row(r) for r in results["matched"]]).sort_values("Lightspeed Time")
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
                "Difference (Valor - Lightspeed)": r["amount_diff"],
                "Lightspeed Time": r["lightspeed"]["timestamp"],
                "Valor Time": r["closest_valor"]["timestamp"],
            }
            for r in results["likely_mismatch"]
        ]).to_excel(writer, index=False, sheet_name="Likely Mismatch")

        pd.DataFrame([
            {"Valor Amount": r["base_amount"], "Card": f"{r['card_scheme']} {r['masked_card']}", "Time": r["timestamp"]}
            for r in results["unexplained_valor_charge"]
        ]).to_excel(writer, index=False, sheet_name="Unexplained Valor Charge")

        pd.DataFrame([_matched_row(r) for r in results["matched"]]).to_excel(
            writer, index=False, sheet_name="Matched"
        )
    buffer.seek(0)

    st.download_button(
        label="Download full report as Excel",
        data=buffer,
        file_name=f"reconciliation_{selected_label}_{start_date}_{end_date}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

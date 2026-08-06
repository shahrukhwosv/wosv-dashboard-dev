"""
Employee Commission Report Generator

This is a Streamlit "page" routed by the top-level app.py. Pick a store,
pick a date range, click Generate Report. Downloads as Excel.
"""

import io
import json
from datetime import date, timedelta

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd

from lightspeed_client import (
    load_config,
    fetch_employees,
    fetch_discounts,
    fetch_sales,
    normalize_sale,
)
from commission_engine import load_rules, load_stores_meta, calculate_commissions

st.title("Employee Commission Report Generator")

config = load_config()
stores = config["stores"]
rules = load_rules()
stores_meta = load_stores_meta()

connected_stores = {
    key: val for key, val in stores.items()
    if val.get("refresh_token") and not val.get("pace_only")
}

if not connected_stores:
    st.error(
        "No stores are connected yet. Run `python oauth_setup.py store_1` "
        "(through store_10) in your terminal first — see README.md."
    )
    st.stop()

col1, col2 = st.columns([1, 2])

with col1:
    st.subheader("1. Choose scope")

    store_options = ["All connected stores"] + sorted(
        [v["name"] for v in connected_stores.values()],
        key=str.casefold,
    )

    selected_store_label = st.selectbox("Store", store_options)

    default_start = date.today() - timedelta(days=14)
    start_date = st.date_input("Period start", value=default_start)
    end_date = st.date_input("Period end", value=date.today())

    if "generate_requested" not in st.session_state:
        st.session_state.generate_requested = False

    if st.button("Generate Report", type="primary"):
        st.session_state.generate_requested = True

    generate = st.session_state.generate_requested

with col2:
    st.subheader("Active commission rules")
    for r in rules:
        status = "✅" if r.get("enabled", True) else "⏸️ disabled"
        # Show only the user-facing rule wording. Escape dollar signs so
        # Streamlit does not interpret text between them as a math formula.
        display_text = r["description"].replace("$", r"\$")
        st.markdown(display_text)

if generate:
    if start_date > end_date:
        st.error("Period start must be before period end.")
        st.stop()

    if selected_store_label == "All connected stores":
        target_stores = list(connected_stores.keys())
    else:
        target_stores = [
            k for k, v in connected_stores.items() if v["name"] == selected_store_label
        ]

    all_transactions = []
    progress = st.progress(0.0, text="Starting...")

    for i, store_key in enumerate(target_stores):
        store_name = connected_stores[store_key]["name"]
        progress.progress(
            i / len(target_stores), text=f"Pulling data for {store_name}..."
        )

        try:
            employees = fetch_employees(config, store_key)
            discount_names_by_id = fetch_discounts(config, store_key)
            raw_sales = fetch_sales(config, store_key, start_date, end_date)
        except Exception as e:
            st.warning(f"Could not pull data for {store_name}: {e}")
            continue

        # Gather every promo name any enabled rule cares about, so we track
        # all of them (e.g. both the PUFF promo and the 3 B4G1 promo names)
        all_promo_names = []
        for r in rules:
            if r["type"] == "promo_applied" and r.get("enabled", True):
                names = r["promo_name_match"]
                if isinstance(names, str):
                    names = [names]
                all_promo_names.extend(names)

        for sale in raw_sales:
            all_transactions.append(
                normalize_sale(
                    sale,
                    employees,
                    store_key,
                    all_promo_names,
                    discount_names_by_id,
                )
            )

    progress.progress(1.0, text="Calculating commissions...")

    if not all_transactions:
        st.warning("No transactions found for that store/date range.")
        st.stop()

    report_df = calculate_commissions(all_transactions, rules, stores_meta)
    progress.empty()

    # Save the completed report so it remains visible across Streamlit reruns.
    st.session_state.report_df = report_df
    st.session_state.report_start_date = start_date
    st.session_state.report_end_date = end_date
    st.session_state.generate_requested = False

    # Force one automatic rerun after calculation so the saved report is
    # rendered immediately. The user does not need to click a second time.
    st.rerun()

# Render the most recently completed report outside the button block.
if "report_df" in st.session_state:
    report_df = st.session_state.report_df
    report_start_date = st.session_state.report_start_date
    report_end_date = st.session_state.report_end_date

    st.subheader("2. Report")

    # Show commission dollar values instead of internal count columns.
    # Find the $100-sale payout column from the enabled threshold rule instead
    # of assuming that the rule ID is always "big_sale_bonus".
    threshold_rule = next(
        (
            rule
            for rule in rules
            if rule.get("enabled", True)
            and rule.get("type") == "per_transaction_threshold"
        ),
        None,
    )
    threshold_payout_column = (
        f"{threshold_rule['id']}_payout" if threshold_rule else None
    )

    display_df = pd.DataFrame({
        "Store": report_df["store"],
        "Employee": report_df["employee_name"],
        "Sales": report_df["transaction_count"],
        "$100+ Sales": (
            report_df.get(threshold_payout_column, 0.0)
            if threshold_payout_column
            else 0.0
        ),
        "5000+ Puff": report_df.get("puff_promo_payout", 0.0),
        "B4G1": report_df.get("B4G1_payout", 0.0),
        "Total Commission": report_df["total_commission"],
    })

    currency_columns = ["$100+ Sales", "5000+ Puff", "B4G1", "Total Commission"]
    st.dataframe(
        display_df.style.format({column: "${:,.2f}" for column in currency_columns}),
        width="stretch",
        hide_index=True,
    )

    # Copy the report as tab-separated raw values so it pastes cleanly into
    # Excel or Google Sheets. Dollar signs and display formatting are excluded.
    def clipboard_value(value):
        if pd.isna(value):
            return ""
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    clipboard_lines = ["\t".join(display_df.columns)]
    for row in display_df.itertuples(index=False, name=None):
        clipboard_lines.append("\t".join(clipboard_value(value) for value in row))
    clipboard_text = "\n".join(clipboard_lines)

    components.html(
        f"""
        <div style="display:flex;align-items:center;gap:10px;font-family:Arial,sans-serif;">
          <button id="copy-table-button" style="
              background:#ffffff;
              color:#262730;
              border:1px solid rgba(49,51,63,.2);
              border-radius:8px;
              padding:8px 14px;
              font-size:14px;
              cursor:pointer;
          ">📋 Copy Table</button>
          <span id="copy-table-status" style="font-size:14px;"></span>
        </div>
        <script>
          const tableText = {json.dumps(clipboard_text)};
          const button = document.getElementById("copy-table-button");
          const status = document.getElementById("copy-table-status");

          async function copyTable() {{
            try {{
              await navigator.clipboard.writeText(tableText);
              status.textContent = "Copied!";
            }} catch (error) {{
              const textarea = document.createElement("textarea");
              textarea.value = tableText;
              textarea.style.position = "fixed";
              textarea.style.opacity = "0";
              document.body.appendChild(textarea);
              textarea.focus();
              textarea.select();
              document.execCommand("copy");
              textarea.remove();
              status.textContent = "Copied!";
            }}
            setTimeout(() => status.textContent = "", 1800);
          }}

          button.addEventListener("click", copyTable);
        </script>
        """,
        height=48,
    )

    st.metric("Total commissions owed", f"${report_df['total_commission'].sum():,.2f}")

    # Excel download uses the same clean headers and dollar-value columns.
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        display_df.to_excel(writer, index=False, sheet_name="Commissions")
        worksheet = writer.sheets["Commissions"]
        for column_index in range(4, 8):
            for cell in worksheet.iter_cols(
                min_col=column_index, max_col=column_index, min_row=2
            ):
                for value_cell in cell:
                    value_cell.number_format = '$#,##0.00'
    buffer.seek(0)

    st.download_button(
        label="Download as Excel",
        data=buffer,
        file_name=f"commission_report_{report_start_date}_{report_end_date}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

"""
Top Shelf Invoices page.

Shows Top Shelf Novelties (wholesale) invoices for a chosen date or date
range - invoice #, customer, amount, shipping cost, profit and margin -
read from the "Top Shelf Invoices" Google Sheet tab that the nightly job
(nightly_refresh.py -> topshelf_invoices.nightly_sync) keeps up to date.
See topshelf_invoices.py for exactly how each number is calculated.

Admins also get:
  - "Connect ERP": the one-time login + emailed code for the ERP account
    the nightly job uses (see topshelf_erp.py).
  - "Re-pull from ERP": refreshes the selected dates right now, e.g. after
    an invoice was edited or a late shipping label was printed.
"""
from datetime import timedelta

import pandas as pd
import streamlit as st

import shipstation_client
import topshelf_erp
from sales_pace import store_local_today
from topshelf_invoices import read_log, sync_days

st.title("Top Shelf Invoices")

yesterday = store_local_today() - timedelta(days=1)


@st.cache_data(ttl=600)
def _load_log():
    return read_log()


# ---------------------------------------------------------------------------
# Date selection
# ---------------------------------------------------------------------------

c1, c2 = st.columns([2, 1])
with c1:
    picked = st.date_input(
        "Date or date range",
        value=(yesterday, yesterday),
        max_value=yesterday,
        help="Pick one day, or click a start and end date for a range.",
    )
with c2:
    st.write("")
    st.write("")
    if st.button("Reload from sheet"):
        st.cache_data.clear()

if isinstance(picked, (tuple, list)):
    start = picked[0]
    end = picked[1] if len(picked) > 1 else picked[0]
else:
    start = end = picked

df = _load_log()
period = df[(df["Date"] >= start) & (df["Date"] <= end)] if not df.empty else df

label = start.strftime("%b %-d, %Y") if start == end else f"{start.strftime('%b %-d')} - {end.strftime('%b %-d, %Y')}"
st.subheader(label)

if period.empty:
    st.info("No invoices logged for this period.")
else:
    sales = period["Product Sales"].sum()
    profit = period["Profit"].sum()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Invoices", f"{len(period):,}")
    m2.metric("Amount", f"${period['Amount'].sum():,.2f}")
    m3.metric("Profit", f"${profit:,.2f}")
    m4.metric("Margin", f"{profit / sales * 100:.1f}%" if sales > 0 else "-")

    show = period.sort_values(["Date", "Invoice #"])
    cols = ["Invoice #", "Customer", "Amount", "Shipping", "Profit", "Margin %"]
    if start != end:
        cols = ["Date"] + cols
    table = show[cols].copy()
    table["Margin %"] = table["Margin %"].apply(lambda m: "\u2014" if pd.isna(m) else f"{m:.1f}%")
    st.dataframe(
        table,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Invoice #": st.column_config.NumberColumn(format="%d"),
            "Amount": st.column_config.NumberColumn(format="dollar"),
            "Shipping": st.column_config.NumberColumn(format="dollar", help="0 when the invoice charged shipping; otherwise the ShipStation label cost"),
            "Profit": st.column_config.NumberColumn(format="dollar", help="Product sales - product cost - shipping"),
            "Margin %": st.column_config.TextColumn(help="Profit / product sales (amount without tax or shipping charged)"),
        },
    )

    problems = period[period["Shipping Source"].isin(["Not found in ShipStation", "ShipStation not connected"])]
    if not problems.empty:
        st.warning(
            f"{len(problems)} invoice(s) had no shipping charged and no ShipStation label found yet "
            f"({', '.join(str(i) for i in problems['Invoice #'])}) - their shipping is counted as $0. "
            "Labels printed after the invoice are picked up automatically over the next nights."
        )

    with st.expander("All columns"):
        st.dataframe(show, hide_index=True, use_container_width=True)
        st.download_button(
            "Download CSV",
            show.to_csv(index=False).encode(),
            file_name=f"top_shelf_invoices_{start.isoformat()}_{end.isoformat()}.csv",
            mime="text/csv",
        )


# ---------------------------------------------------------------------------
# Admin tools
# ---------------------------------------------------------------------------

if st.session_state.get("is_admin"):
    st.divider()
    with st.expander("Admin: ERP connection and re-pull"):
        status = None
        try:
            status = topshelf_erp.connection_status()
        except Exception as e:
            st.error(f"Couldn't read ERP connection status: {e}")

        if status:
            exp = status["refresh_expires"]
            st.write(
                f"ERP connected as **{status['username'] or 'unknown'}** - last refreshed "
                f"{status['updated_at']:%b %-d %I:%M %p} UTC"
                + (f", login lapses {exp:%b %-d %I:%M %p} UTC unless the nightly job runs before then." if exp else ".")
            )
        else:
            st.warning("ERP not connected yet - the nightly job can't pull invoices until you connect it below.")

        st.write("ShipStation: " + ("connected." if shipstation_client.is_configured() else
                 "**not connected** - set SHIPSTATION_API_KEY and SHIPSTATION_API_SECRET in Railway."))

        st.markdown("**Connect ERP** (use the dedicated read-only ERP login)")
        pending = st.session_state.get("erp_pending_login")
        if not pending:
            with st.form("erp_login"):
                u = st.text_input("ERP email")
                p = st.text_input("ERP password", type="password")
                if st.form_submit_button("Send code"):
                    try:
                        st.session_state.erp_pending_login = topshelf_erp.start_login(u, p)
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
        else:
            with st.form("erp_otp"):
                st.write(f"A one-time code was sent for **{pending['username']}**.")
                code = st.text_input("Code")
                ok = st.form_submit_button("Connect")
                cancel = st.form_submit_button("Cancel")
            if cancel:
                del st.session_state.erp_pending_login
                st.rerun()
            if ok:
                try:
                    topshelf_erp.finish_login(pending, code)
                    del st.session_state.erp_pending_login
                    st.success("ERP connected.")
                except Exception as e:
                    st.error(str(e))

        st.markdown("**Re-pull from ERP**")
        n_days = (end - start).days + 1
        st.caption(f"Re-pulls {label} ({n_days} day(s)) from the ERP and ShipStation and replaces those days in the sheet.")
        if st.button("Re-pull selected dates", disabled=n_days > 31):
            with st.spinner("Pulling from the ERP..."):
                try:
                    count = sync_days([start + timedelta(days=i) for i in range(n_days)], progress=lambda m: None)
                    st.cache_data.clear()
                    st.success(f"Done - {count} invoice(s) written.")
                except Exception as e:
                    st.error(f"Re-pull failed: {e}")

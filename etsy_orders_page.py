"""
Etsy Orders page.

Profit/loss for each Etsy order over a chosen date or date range - total,
Etsy fees, ShipStation shipping, Top Shelf ERP product cost, profit and
margin - read from the "Etsy Orders" Google Sheet tab that the nightly job
(nightly_refresh.py -> etsy_orders.nightly_sync) keeps up to date. See
etsy_orders.py for exactly how each number is calculated.

Admins also get:
  - "Connect Etsy": the one-time Etsy approval (see etsy_client.py).
  - "Re-pull from Etsy": refreshes the selected dates right now.
"""
from datetime import timedelta

import pandas as pd
import streamlit as st

import etsy_client
import shipstation_client
from etsy_orders import COST_OK, read_log, read_other_fees, sync_days
from sales_pace import store_local_today

st.title("Etsy Orders")

# ---------------------------------------------------------------------------
# Etsy sends the admin back here with ?code=...&state=... after approval.
# ---------------------------------------------------------------------------

qp = st.query_params
if "code" in qp and "state" in qp:
    if st.session_state.get("is_admin"):
        try:
            shop = etsy_client.finish_login(qp["code"], qp["state"])
            st.success(f"Etsy connected: {shop}")
        except Exception as e:
            st.error(f"Couldn't finish connecting Etsy: {e}")
    st.query_params.clear()
elif "error" in qp:
    st.error(f"Etsy didn't connect: {qp.get('error_description') or qp['error']}")
    st.query_params.clear()

yesterday = store_local_today() - timedelta(days=1)


@st.cache_data(ttl=600)
def _load():
    return read_log(), read_other_fees()


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

try:
    df, other = _load()
except Exception as e:
    st.error(f"Couldn't read the Etsy log from Google Sheets: {e}")
    st.stop()

period = df[(df["Date"] >= start) & (df["Date"] <= end)] if not df.empty else df
other_period = other[(other["Date"] >= start) & (other["Date"] <= end)] if not other.empty else other

label = start.strftime("%b %-d, %Y") if start == end else f"{start.strftime('%b %-d')} - {end.strftime('%b %-d, %Y')}"
st.subheader(label)

if period.empty:
    st.info("No Etsy orders logged for this period.")
else:
    revenue = period["Revenue"].sum()
    profit = period["Profit"].sum()
    other_fees = other_period["Amount"].sum() if not other_period.empty else 0.0

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Orders", f"{len(period):,}")
    m2.metric("Revenue", f"${revenue:,.2f}", help="Total paid by buyers, minus sales tax and refunds")
    m3.metric("Etsy fees", f"${period['Etsy Fees'].sum():,.2f}")
    m4.metric("Profit", f"${profit:,.2f}")
    m5.metric("Margin", f"{profit / revenue * 100:.1f}%" if revenue > 0 else "-")

    if other_fees:
        st.caption(
            f"Other Etsy fees in this period not tied to an order (new listings, renewals, etc.): "
            f"**${other_fees:,.2f}** - profit after those: **${profit - other_fees:,.2f}**"
        )

    show = period.sort_values(["Date", "Order #"])
    cols = ["Order #", "Buyer", "Total", "Etsy Fees", "Shipping", "Product Cost", "Profit", "Margin %"]
    if start != end:
        cols = ["Date"] + cols
    table = show[cols].copy()
    table["Margin %"] = table["Margin %"].apply(lambda m: "—" if pd.isna(m) else f"{m:.1f}%")
    st.dataframe(
        table,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Order #": st.column_config.NumberColumn(format="%d"),
            "Total": st.column_config.NumberColumn(format="dollar", help="What the buyer paid, including shipping and sales tax"),
            "Etsy Fees": st.column_config.NumberColumn(format="dollar", help="Transaction, processing, offsite ads and other fees Etsy charged for this order"),
            "Shipping": st.column_config.NumberColumn(format="dollar", help="ShipStation label cost"),
            "Product Cost": st.column_config.NumberColumn(format="dollar", help="Top Shelf ERP cost x quantity (Etsy SKU = ERP UPC)"),
            "Profit": st.column_config.NumberColumn(format="dollar", help="Total - tax - refunds - Etsy fees - shipping - product cost"),
            "Margin %": st.column_config.TextColumn(help="Profit / revenue (total without sales tax or refunds)"),
        },
    )

    no_cost = period[period["Cost Status"] != COST_OK]
    if not no_cost.empty:
        st.warning(
            f"{len(no_cost)} order(s) have item(s) with no Top Shelf cost - that cost is counted as $0, "
            "so their profit is overstated. Fix by setting the Etsy SKU to the product's ERP UPC:\n\n"
            + "\n".join(f"- {int(r['Order #'])}: {r['Cost Status'].removeprefix('Missing: ')}" for _, r in no_cost.iterrows())
        )
    no_ship = period[period["Shipping Source"] != "ShipStation"]
    if not no_ship.empty:
        st.warning(
            f"{len(no_ship)} order(s) have no ShipStation label yet "
            f"({', '.join(str(int(i)) for i in no_ship['Order #'])}) - shipping counted as $0. "
            "Labels printed later are picked up automatically over the next nights."
        )

    with st.expander("All columns"):
        st.dataframe(show, hide_index=True, use_container_width=True)
        st.download_button(
            "Download CSV",
            show.to_csv(index=False).encode(),
            file_name=f"etsy_orders_{start.isoformat()}_{end.isoformat()}.csv",
            mime="text/csv",
        )
    if not other_period.empty:
        with st.expander("Other Etsy fees"):
            st.dataframe(other_period.sort_values("Date"), hide_index=True, use_container_width=True)


# ---------------------------------------------------------------------------
# Admin tools
# ---------------------------------------------------------------------------

if st.session_state.get("is_admin"):
    st.divider()
    with st.expander("Admin: Etsy connection and re-pull"):
        if not etsy_client.is_configured():
            st.warning("Etsy isn't set up - add ETSY_API_KEY, ETSY_SHARED_SECRET and ETSY_REDIRECT_URI in Railway "
                       "(see etsy_client.py for the steps).")
        else:
            status = None
            try:
                status = etsy_client.connection_status()
            except Exception as e:
                st.error(f"Couldn't read Etsy connection status: {e}")
            if status:
                exp = status["refresh_expires"]
                st.write(
                    f"Etsy connected: **{status['shop_name'] or status['shop_id']}** - last refreshed "
                    f"{status['updated_at']:%b %-d %I:%M %p} UTC"
                    + (f", connection lapses {exp:%b %-d, %Y} unless the nightly job runs before then." if exp else ".")
                )
            else:
                st.warning("Etsy not connected yet - the nightly job can't pull orders until you connect it below.")

            if st.button("Connect Etsy" if not status else "Reconnect Etsy"):
                try:
                    st.session_state.etsy_auth_url = etsy_client.start_login()
                except Exception as e:
                    st.error(str(e))
            if st.session_state.get("etsy_auth_url"):
                st.link_button("Approve on Etsy", st.session_state.etsy_auth_url)
                st.caption("Log in to Etsy as the shop owner. You'll come back to this page connected. "
                           "If you land on a page that doesn't load, paste its full address here:")
                pasted = st.text_input("Redirect address", key="etsy_pasted_redirect")
                if pasted and st.button("Finish connecting"):
                    code, state, err = etsy_client.parse_redirect(pasted)
                    if err or not code:
                        st.error(f"That address has no approval code ({err or 'missing code'}).")
                    else:
                        try:
                            st.success(f"Etsy connected: {etsy_client.finish_login(code, state)}")
                            del st.session_state.etsy_auth_url
                        except Exception as e:
                            st.error(str(e))

        st.write("ShipStation: " + ("connected." if shipstation_client.is_configured() else
                 "**not connected** - set SHIPSTATION_API_KEY and SHIPSTATION_API_SECRET in Railway."))

        st.markdown("**Re-pull from Etsy**")
        n_days = (end - start).days + 1
        st.caption(f"Re-pulls {label} ({n_days} day(s)) from Etsy, ShipStation and the ERP and replaces those days in the sheet.")
        if st.button("Re-pull selected dates", disabled=n_days > 31):
            with st.spinner("Pulling from Etsy..."):
                try:
                    count = sync_days([start + timedelta(days=i) for i in range(n_days)], progress=lambda m: None)
                    st.cache_data.clear()
                    st.success(f"Done - {count} order(s) written.")
                except Exception as e:
                    st.error(f"Re-pull failed: {e}")

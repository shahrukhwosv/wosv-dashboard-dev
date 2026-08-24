"""
App entry point / router.

Requires login (see auth.py) before showing any page. Regular users only
see stores explicitly granted to them (see store_access.py) - admins see
every connected store automatically. Both "Manage Users" and "Add Store"
only appear in the sidebar for admin accounts.

Run with:  streamlit run app.py

The actual page content lives in commission_page.py, pace_calculator_page.py,
add_store_page.py, and manage_users_page.py. dashboard_page.py,
pace_calculator_page.py, reconciliation_page.py, and touch_tell_page.py
still exist in the repo but are TEMPORARILY not linked in the sidebar -
re-add them to the `pages` list below when ready.

NOTE: pace_calculator_page.py has its own separate simple password gate
built in (see PACE_CALCULATOR_PASSWORD) - unrelated to the login system
here, kept as-is since it predates user accounts.
"""

import streamlit as st

from auth import require_login

st.set_page_config(page_title="WOSV Dashboard", layout="wide")

require_login()

with st.sidebar:
    st.caption(f"Logged in as **{st.session_state.username}**")
    if st.button("Log out"):
        for key in ("user_id", "username", "is_admin"):
            st.session_state.pop(key, None)
        st.rerun()

commissions_page = st.Page(
    "commission_page.py", title="Commissions", icon="💰", default=True
)
transactions_page = st.Page(
    "reconciliation_page.py", title="Transactions", icon="💳"
)

pages = [
    commissions_page,
    transactions_page,
]

if st.session_state.get("is_admin"):
    pages.append(
        st.Page("add_store_page.py", title="Add Store", icon="➕")
    )
    pages.append(
        st.Page("manage_users_page.py", title="Manage Users", icon="👤")
    )

pg = st.navigation({"WOSV Dashboard": pages})
pg.run()

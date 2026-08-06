"""
App entry point / router.

Requires login (see auth.py) before showing any page. Each user only sees
the stores they've personally added (enforced in commission_page.py and
reconciliation_page.py by filtering on owner_user_id). The "Manage Users"
page only appears in the sidebar for admin accounts.

Run with:  streamlit run app.py

The actual page content lives in commission_page.py, reconciliation_page.py,
touch_tell_page.py, pace_calculator_page.py, add_store_page.py, and
manage_users_page.py.

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
    "commission_page.py", title="Commissions", icon="💰"
)
transactions_page = st.Page(
    "reconciliation_page.py", title="Transactions", icon="💳"
)
touch_tell_page = st.Page(
    "touch_tell_page.py", title="Touch Tell", icon="📦"
)
pace_calculator_page = st.Page(
    "pace_calculator_page.py", title="Pace Calculator", icon="📈"
)
add_store_page = st.Page(
    "add_store_page.py", title="Add Store", icon="➕"
)

pages = [
    commissions_page,
    transactions_page,
    touch_tell_page,
    pace_calculator_page,
    add_store_page,
]

if st.session_state.get("is_admin"):
    pages.append(
        st.Page("manage_users_page.py", title="Manage Users", icon="👤")
    )

pg = st.navigation({"WOSV Dashboard": pages})
pg.run()

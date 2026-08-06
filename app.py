"""
App entry point / router.

This file's ONLY job is to set up the page and switch between tools using
Streamlit's navigation API, which lets us control the exact sidebar labels
instead of Streamlit's default file-name-based labels.

Run with:  streamlit run app.py

The actual page content lives in commission_page.py, reconciliation_page.py,
touch_tell_page.py, and pace_calculator_page.py.

NOTE: pace_calculator_page.py has its own simple password gate built in
(see PACE_CALCULATOR_PASSWORD) - no login system here at the app level.
"""

import streamlit as st

st.set_page_config(page_title="WOSV Dashboard", layout="wide")

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

pg = st.navigation({
    "WOSV Dashboard": [
        commissions_page,
        transactions_page,
        touch_tell_page,
        pace_calculator_page,
    ]
})
pg.run()

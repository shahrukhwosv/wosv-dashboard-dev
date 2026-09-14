"""
App entry point / router.

Requires login (see auth.py) before showing any page. Regular users only
see stores explicitly granted to them (see store_access.py) within a
page, AND only see the pages themselves that have been granted to them
(see page_access.py) - admins see every page and every connected store
automatically. "Manage Users" and "Add Store" only appear in the sidebar
for admin accounts, unaffected by page_access.py grants.

Run with:  streamlit run app.py

The actual page content lives in dashboard_page.py, commission_page.py,
reconciliation_page.py, touch_tell_page.py, pace_calculator_page.py,
category_sales_page.py, monthly_reports_page.py,
purchase_order_status_page.py, add_store_page.py, and manage_users_page.py.

NOTE: pace_calculator_page.py has its own separate simple password gate
built in (see PACE_CALCULATOR_PASSWORD) - unrelated to the login system
here, kept as-is since it predates user accounts.
"""

import streamlit as st

from auth import require_login, log_out
from page_access import get_accessible_pages, ALL_PAGE_KEYS

st.set_page_config(page_title="WOSV Dashboard", layout="wide")

require_login()

st.logo("assets/logo.png", size="large")

# Keys here must match page_access.PAGE_REGISTRY - that's what the Manage
# Users page's "Page access" editor grants/revokes per user. Icons use
# Streamlit's built-in Material Symbols support (icon=":material/name:")
# instead of emoji, for a visually consistent icon set.
page_objects = {
    "dashboard": st.Page("dashboard_page.py", title="Dashboard", icon=":material/dashboard:"),
    "commissions": st.Page("commission_page.py", title="Commissions", icon=":material/payments:"),
    "transactions": st.Page("reconciliation_page.py", title="Transactions", icon=":material/receipt_long:"),
    "touch_tell": st.Page("touch_tell_page.py", title="Touch Tell", icon=":material/inventory_2:"),
    "pace_calculator": st.Page("pace_calculator_page.py", title="Pace Calculator", icon=":material/trending_up:"),
    "category_sales": st.Page("category_sales_page.py", title="Category Sales", icon=":material/search:"),
    "monthly_reports": st.Page("monthly_reports_page.py", title="Monthly Reports", icon=":material/summarize:"),
    "purchase_order_status": st.Page("purchase_order_status_page.py", title="Purchase Order Status", icon=":material/assignment:"),
}

if st.session_state.get("is_admin"):
    visible_page_keys = ALL_PAGE_KEYS
else:
    granted = get_accessible_pages(st.session_state.user_id)
    # No rows saved yet for this user = not yet configured, not "grant
    # nothing" - every existing non-admin account currently sees every
    # page (this restriction system is new), so defaulting empty-to-zero
    # here would silently lock out every current user the moment this
    # deploys. Falls back to full access until an admin explicitly saves
    # at least one page for them via Manage Users.
    visible_page_keys = ALL_PAGE_KEYS if not granted else [
        key for key in ALL_PAGE_KEYS if key in granted
    ]

# First page in this list becomes the default landing page (Streamlit
# falls back to the first page automatically when none is marked
# default=True) - deliberately not hardcoding a specific page as default
# any more, since a fixed default could be a page this particular user
# doesn't have access to.
pages = [page_objects[key] for key in visible_page_keys]

if not pages:
    st.info(
        "Your account doesn't have access to any pages yet. "
        "Contact an admin to grant access from Manage Users."
    )
    st.stop()

if st.session_state.get("is_admin"):
    pages.append(
        st.Page("add_store_page.py", title="Add Store", icon=":material/add_business:")
    )
    pages.append(
        st.Page("manage_users_page.py", title="Manage Users", icon=":material/manage_accounts:")
    )

pg = st.navigation({"WOSV Dashboard": pages})

with st.sidebar:
    # Rendered after st.navigation() so it appears below the nav list -
    # Streamlit's sidebar has no official "pin to bottom of viewport" API,
    # so this is "last in the sidebar" rather than glued to the exact
    # bottom of the screen.
    st.divider()
    role_label = "Admin" if st.session_state.get("is_admin") else "User"
    st.markdown(
        f"""
        <div style="line-height: 1.3; margin-bottom: 0.5rem;">
            <div style="font-size: 0.9rem; font-weight: 500;">{st.session_state.username}</div>
            <div style="font-size: 0.75rem; color: var(--text-secondary, #9CA3AF);">{role_label}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("Log out", use_container_width=True):
        log_out()
        st.rerun()

pg.run()

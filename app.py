"""
App entry point / router.

Requires login (see auth.py) before showing any page. Regular users only
see stores explicitly granted to them (see store_access.py) within a
page, AND only see the pages themselves that have been granted to them
(see page_access.py) - admins see every page and every connected store
automatically. "Manage Users" and "Add Store" only appear in the sidebar
for admin accounts, unaffected by page_access.py grants.

Run with:  streamlit run app.py

The actual page content lives in commission_page.py, reconciliation_page.py,
touch_tell_page.py, pace_calculator_page.py, category_sales_page.py,
monthly_reports_page.py, add_store_page.py, and manage_users_page.py.
dashboard_page.py still exists in the repo but is TEMPORARILY not linked
in the sidebar - re-add it to the `page_objects` dict below when ready
(and to page_access.PAGE_REGISTRY, if regular users should be able to be
granted access to it).

NOTE: pace_calculator_page.py has its own separate simple password gate
built in (see PACE_CALCULATOR_PASSWORD) - unrelated to the login system
here, kept as-is since it predates user accounts.
"""

import streamlit as st

from auth import require_login
from page_access import get_accessible_pages, ALL_PAGE_KEYS

st.set_page_config(page_title="WOSV Dashboard", layout="wide")

require_login()

with st.sidebar:
    st.caption(f"Logged in as **{st.session_state.username}**")
    if st.button("Log out"):
        for key in ("user_id", "username", "is_admin"):
            st.session_state.pop(key, None)
        st.rerun()

# Keys here must match page_access.PAGE_REGISTRY - that's what the Manage
# Users page's "Page access" editor grants/revokes per user.
page_objects = {
    "commissions": st.Page("commission_page.py", title="Commissions", icon="💰"),
    "transactions": st.Page("reconciliation_page.py", title="Transactions", icon="💳"),
    "touch_tell": st.Page("touch_tell_page.py", title="Touch Tell", icon="📦"),
    "pace_calculator": st.Page("pace_calculator_page.py", title="Pace Calculator", icon="📈"),
    "category_sales": st.Page("category_sales_page.py", title="Category Sales", icon="🔍"),
    "monthly_reports": st.Page("monthly_reports_page.py", title="Monthly Reports", icon="🗂️"),
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
        st.Page("add_store_page.py", title="Add Store", icon="➕")
    )
    pages.append(
        st.Page("manage_users_page.py", title="Manage Users", icon="👤")
    )

pg = st.navigation({"WOSV Dashboard": pages})
pg.run()

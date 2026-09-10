"""
Manage Users page - admin-only. Lets the admin create new blank-slate user
accounts, grant/revoke which stores each user can see, and delete accounts.
Regular (non-admin) users never see this page in the sidebar.
"""

import streamlit as st

from auth import create_user, list_users, delete_user
from lightspeed_client import load_config
from store_access import (
    get_accessible_store_keys,
    set_user_access,
    get_store_list,
    set_store_list,
    STANDARD_STORES_LIST,
    PACE_CALCULATOR_STORES_LIST,
    DEFAULT_STANDARD_STORE_KEYS,
)
from page_access import get_accessible_pages, set_user_page_access, PAGE_REGISTRY, ALL_PAGE_KEYS

st.title("Manage Users")

if not st.session_state.get("is_admin"):
    st.error("This page is only available to admin accounts.")
    st.stop()

st.subheader("Create a new user")
with st.form("create_user_form", clear_on_submit=True):
    new_username = st.text_input("Username")
    new_password = st.text_input("Temporary password", type="password")
    make_admin = st.checkbox("Make this user an admin too")
    submitted = st.form_submit_button("Create User")

if submitted:
    if not new_username or not new_password:
        st.error("Username and password are both required.")
    else:
        try:
            create_user(new_username, new_password, is_admin=make_admin)
            st.success(
                f"✅ Created '{new_username}'. They start with no store "
                f"access (blank slate - grant it below), but can see every "
                f"page by default until you restrict that separately below too."
            )
        except ValueError as e:
            st.error(str(e))

st.divider()
st.subheader("Store access")
config = load_config()
all_stores = {
    key: val.get("name", key)
    for key, val in config["stores"].items()
    if val.get("refresh_token")
}
users = list_users()
non_admin_users = [u for u in users if not u[2]]  # (id, username, is_admin, created_at)

if not all_stores:
    st.write("No stores connected yet - connect some from the Add Store page first.")
elif not non_admin_users:
    st.write("No non-admin users yet - admins automatically see every store.")
else:
    username_to_id = {u[1]: u[0] for u in non_admin_users}
    selected_username = st.selectbox("Choose a user", list(username_to_id.keys()))
    selected_user_id = username_to_id[selected_username]

    current_access = get_accessible_store_keys(selected_user_id)
    store_keys_sorted = sorted(all_stores.keys(), key=lambda k: all_stores[k])
    default_keys = [k for k in store_keys_sorted if k in current_access]

    selected_keys = st.multiselect(
        f"Stores {selected_username} can see",
        options=store_keys_sorted,
        default=default_keys,
        format_func=lambda key: all_stores.get(key, key),
    )

    if st.button("Save access", type="primary"):
        set_user_access(selected_user_id, set(selected_keys))
        st.success(f"Updated access for {selected_username}.")
        st.rerun()

    st.markdown("**Page access**")
    st.caption(
        "Which pages appear in the sidebar for this user. Until saved here "
        "for the first time, a user sees every page (today's behavior) - "
        "check only the ones they should have, then Save. Note: there's "
        "currently no way to restrict a user to literally zero pages "
        "through this control (an empty save is treated the same as "
        "'not yet configured' and falls back to everything); ask if you "
        "need that and it can be added."
    )
    current_pages = get_accessible_pages(selected_user_id)
    default_pages = ALL_PAGE_KEYS if not current_pages else [
        key for key in ALL_PAGE_KEYS if key in current_pages
    ]

    selected_page_keys = st.multiselect(
        f"Pages {selected_username} can see",
        options=ALL_PAGE_KEYS,
        default=default_pages,
        format_func=lambda key: dict(PAGE_REGISTRY).get(key, key),
        key="page_access_multiselect",
    )

    if st.button("Save page access", type="primary", key="save_page_access"):
        set_user_page_access(selected_user_id, set(selected_page_keys))
        st.success(f"Updated page access for {selected_username}.")
        st.rerun()

st.divider()
st.subheader("Standard Stores list")
st.caption(
    "Commissions, Transactions, Touch Tell, and Monthly Reports only show "
    "stores from this list (further narrowed by each user's own access "
    "above) - Category Sales ignores this and always shows everything a "
    "user/admin can otherwise see."
)
if not all_stores:
    st.write("No stores connected yet - connect some from the Add Store page first.")
else:
    store_keys_sorted = sorted(all_stores.keys(), key=lambda k: all_stores[k])
    current_standard = get_store_list(STANDARD_STORES_LIST, default_keys=DEFAULT_STANDARD_STORE_KEYS)
    default_standard = [k for k in store_keys_sorted if k in current_standard]

    selected_standard_keys = st.multiselect(
        "Stores included in the Standard Stores list",
        options=store_keys_sorted,
        default=default_standard,
        format_func=lambda key: all_stores.get(key, key),
        key="standard_stores_multiselect",
    )

    if st.button("Save Standard Stores list", type="primary", key="save_standard_stores"):
        set_store_list(STANDARD_STORES_LIST, set(selected_standard_keys))
        st.success("Updated the Standard Stores list.")
        st.rerun()

st.divider()
st.subheader("Pace Calculator Stores list")
st.caption(
    "Which stores show up on the Pace Calculator page. Defaults to every "
    "connected store (its current behavior) until saved here for the "
    "first time."
)
if not all_stores:
    st.write("No stores connected yet - connect some from the Add Store page first.")
else:
    store_keys_sorted = sorted(all_stores.keys(), key=lambda k: all_stores[k])
    current_pace = get_store_list(PACE_CALCULATOR_STORES_LIST, default_keys=list(config["stores"].keys()))
    default_pace = [k for k in store_keys_sorted if k in current_pace]

    selected_pace_keys = st.multiselect(
        "Stores included on the Pace Calculator page",
        options=store_keys_sorted,
        default=default_pace,
        format_func=lambda key: all_stores.get(key, key),
        key="pace_stores_multiselect",
    )

    if st.button("Save Pace Calculator Stores list", type="primary", key="save_pace_stores"):
        set_store_list(PACE_CALCULATOR_STORES_LIST, set(selected_pace_keys))
        st.success("Updated the Pace Calculator Stores list.")
        st.rerun()

st.divider()
st.subheader("Existing users")
for user_id, username, is_admin, created_at in list_users():
    label = f"**{username}**" + (" (admin)" if is_admin else "")
    col1, col2 = st.columns([4, 1])
    with col1:
        st.write(f"- {label} — created {created_at.strftime('%b %d, %Y')}")
    with col2:
        confirm_key = f"confirm_delete_{user_id}"
        if st.session_state.get(confirm_key):
            if st.button("Confirm delete", key=f"confirm_btn_{user_id}", type="primary"):
                try:
                    delete_user(user_id, st.session_state.user_id)
                    st.success(f"Deleted '{username}'. Stores they could see are untouched.")
                    st.session_state.pop(confirm_key, None)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        else:
            if st.button("Delete", key=f"delete_btn_{user_id}"):
                st.session_state[confirm_key] = True
                st.rerun()

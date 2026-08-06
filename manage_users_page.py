"""
Manage Users page - admin-only. Lets the admin create new blank-slate user
accounts, grant/revoke which stores each user can see, and delete accounts.
Regular (non-admin) users never see this page in the sidebar.
"""

import streamlit as st

from auth import create_user, list_users, delete_user
from lightspeed_client import load_config
from store_access import get_accessible_store_keys, set_user_access

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
                f"✅ Created '{new_username}'. They start with a blank slate — "
                f"grant them store access below."
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
    store_key_by_name = {name: key for key, name in all_stores.items()}
    current_names = [all_stores[k] for k in current_access if k in all_stores]

    selected_names = st.multiselect(
        f"Stores {selected_username} can see",
        options=list(all_stores.values()),
        default=current_names,
    )

    if st.button("Save access", type="primary"):
        selected_keys = {store_key_by_name[name] for name in selected_names}
        set_user_access(selected_user_id, selected_keys)
        st.success(f"Updated access for {selected_username}.")
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

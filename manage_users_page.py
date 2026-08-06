"""
Manage Users page - admin-only. Lets the admin create new blank-slate user
accounts. Regular (non-admin) users never see this page in the sidebar.
"""

import streamlit as st

from auth import create_user, list_users

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
                f"no stores until they add their own from the Add Store page."
            )
        except ValueError as e:
            st.error(str(e))

st.divider()
st.subheader("Existing users")
for user_id, username, is_admin, created_at in list_users():
    label = f"**{username}**" + (" (admin)" if is_admin else "")
    st.write(f"- {label} — created {created_at.strftime('%b %d, %Y')}")

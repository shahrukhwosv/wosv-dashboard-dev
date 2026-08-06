"""
Authentication for the dashboard: username/password accounts, each user
seeing only the stores they've personally added.

Requires a database (DATABASE_URL) - same one used for store config. If no
database is configured, login is disabled entirely and this raises a clear
error rather than silently allowing everyone in.
"""

import os
import bcrypt

from lightspeed_client import _get_db_connection


def _ensure_users_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
    conn.commit()


def _require_db():
    conn = _get_db_connection()
    if conn is None:
        raise RuntimeError(
            "No database configured (DATABASE_URL is not set) - user "
            "accounts require a database to store usernames/passwords. "
            "Set up the database first (same one used for store config)."
        )
    return conn


def create_user(username, password, is_admin=False):
    """Creates a new user with a blank slate (no stores). Raises ValueError
    if the username is already taken."""
    conn = _require_db()
    try:
        _ensure_users_table(conn)
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO app_users (username, password_hash, is_admin) "
                    "VALUES (%s, %s, %s)",
                    (username, password_hash, is_admin),
                )
            except Exception as e:
                conn.rollback()
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    raise ValueError(f"Username '{username}' is already taken.")
                raise
        conn.commit()
    finally:
        conn.close()


def verify_login(username, password):
    """Returns (user_id, is_admin) on success, or None if the username/
    password don't match."""
    conn = _require_db()
    try:
        _ensure_users_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, password_hash, is_admin FROM app_users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
        if not row:
            return None
        user_id, password_hash, is_admin = row
        if bcrypt.checkpw(password.encode(), password_hash.encode()):
            return user_id, is_admin
        return None
    finally:
        conn.close()


def list_users():
    """Returns [(id, username, is_admin, created_at), ...] for the admin
    user-management page."""
    conn = _require_db()
    try:
        _ensure_users_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, is_admin, created_at FROM app_users "
                "ORDER BY created_at"
            )
            return cur.fetchall()
    finally:
        conn.close()


def delete_user(user_id, requesting_user_id):
    """
    Deletes a user account. Their store access grants are cleaned up
    automatically (user_store_access has ON DELETE CASCADE) - the stores
    themselves are untouched, they just stop being visible to this user.

    Safety checks (raises ValueError):
      - can't delete your own currently-logged-in account
      - can't delete the last remaining admin account
    """
    if user_id == requesting_user_id:
        raise ValueError("You can't delete your own account while logged in as it.")

    conn = _require_db()
    try:
        _ensure_users_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT is_admin FROM app_users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("That user no longer exists.")
            target_is_admin = row[0]

            if target_is_admin:
                cur.execute("SELECT COUNT(*) FROM app_users WHERE is_admin = true")
                admin_count = cur.fetchone()[0]
                if admin_count <= 1:
                    raise ValueError("Can't delete the last remaining admin account.")

            cur.execute("DELETE FROM app_users WHERE id = %s", (user_id,))
        conn.commit()
    finally:
        conn.close()


def require_login():
    """
    Call at the top of app.py before rendering any page. Shows a login form
    if not already logged in (and st.stop()s), or does nothing if already
    logged in. Sets st.session_state.user_id / username / is_admin on
    success.
    """
    import streamlit as st

    if not os.getenv("DATABASE_URL"):
        st.error(
            "No database is configured, so user accounts can't work yet. "
            "Set up the database first (see README.md)."
        )
        st.stop()

    if st.session_state.get("user_id"):
        return  # already logged in

    st.title("WOSV Dashboard — Log In")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log In")

    if submitted:
        result = verify_login(username, password)
        if result:
            user_id, is_admin = result
            st.session_state.user_id = user_id
            st.session_state.username = username
            st.session_state.is_admin = is_admin
            st.rerun()
        else:
            st.error("Incorrect username or password.")

    st.stop()

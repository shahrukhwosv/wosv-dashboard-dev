"""
Authentication for the dashboard: username/password accounts, each user
seeing only the stores they've personally added.

Requires a database (DATABASE_URL) - same one used for store config. If no
database is configured, login is disabled entirely and this raises a clear
error rather than silently allowing everyone in.

SESSION PERSISTENCE: st.session_state alone only survives clicks/reruns
within the same live browser connection - it does NOT survive an actual
page reload (Streamlit starts a brand new, empty session_state every
time). To stay logged in across reloads, a signed random session token is
stored server-side (see the app_sessions table below) and mirrored into a
browser cookie via streamlit_extras.cookie_manager. On each
require_login() call, if session_state doesn't already have a logged-in
user, the cookie is checked (once the component reports it's actually
synced from the browser - see cookie_manager.ready() below) and the
session validated against the database before falling back to the login
form.
"""

import os
import secrets
import hashlib
from datetime import datetime, timedelta, timezone

import bcrypt

from lightspeed_client import _get_db_connection

SESSION_COOKIE_NAME = "wosv_session"
SESSION_LIFETIME_DAYS = 30


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


def _ensure_sessions_table(conn):
    _ensure_users_table(conn)  # FK dependency - must exist first
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS app_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL
            )
            """
        )
    conn.commit()


def _hash_token(token):
    # Session tokens are already high-entropy random values (unlike
    # passwords), so a fast SHA-256 hash is sufficient here - this is
    # purely to avoid storing the raw, usable token in the database.
    return hashlib.sha256(token.encode()).hexdigest()


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


def create_session_token(user_id):
    """Creates a new server-side session record and returns the raw token
    to store in the browser cookie (only the hash is kept in the
    database)."""
    conn = _require_db()
    try:
        _ensure_sessions_table(conn)
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_LIFETIME_DAYS)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_sessions (token_hash, user_id, expires_at) "
                "VALUES (%s, %s, %s)",
                (_hash_token(token), user_id, expires_at),
            )
        conn.commit()
        return token
    finally:
        conn.close()


def validate_session_token(token):
    """Returns (user_id, username, is_admin) if token is a live, unexpired
    session, or None otherwise (missing, expired, or revoked)."""
    if not token:
        return None
    conn = _require_db()
    try:
        _ensure_sessions_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.username, u.is_admin
                FROM app_sessions s
                JOIN app_users u ON u.id = s.user_id
                WHERE s.token_hash = %s AND s.expires_at > now()
                """,
                (_hash_token(token),),
            )
            row = cur.fetchone()
        return row if row else None
    finally:
        conn.close()


def revoke_session_token(token):
    """Deletes a session record (used on logout, or when a password
    changes) - safe to call even if the token doesn't exist."""
    if not token:
        return
    conn = _require_db()
    try:
        _ensure_sessions_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app_sessions WHERE token_hash = %s",
                (_hash_token(token),),
            )
        conn.commit()
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
    Deletes a user account. Their store access grants and any active
    login sessions are cleaned up automatically (user_store_access and
    app_sessions both have ON DELETE CASCADE) - the stores themselves are
    untouched, they just stop being visible to this user.

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


def _get_cookie_manager():
    """
    One cookie_manager() call per script run (it's fine to call this
    every run - the component is keyed and reuses its state across
    reruns). Its first read on a given browser session comes back "not
    ready" for a run or two while the underlying component's JS actually
    loads and reports the browser's cookies back to Python - callers MUST
    check .ready() before trusting .get() results, or they'll
    incorrectly treat "hasn't synced yet" as "no session" and always show
    the login form, even for someone with a perfectly valid cookie. This
    was the bug in an earlier version of this function.
    """
    from streamlit_extras.cookie_manager import cookie_manager
    return cookie_manager(key="wosv_cookie_manager")


def require_login():
    """
    Call at the top of app.py before rendering any page. Restores a
    logged-in session from a browser cookie if present and still valid
    (see module docstring), otherwise shows a login form and st.stop()s.
    Sets st.session_state.user_id / username / is_admin on success either
    way.
    """
    import streamlit as st

    if not os.getenv("DATABASE_URL"):
        st.error(
            "No database is configured, so user accounts can't work yet. "
            "Set up the database first (see README.md)."
        )
        st.stop()

    if st.session_state.get("user_id"):
        return  # already logged in this browser session

    cookie_manager = _get_cookie_manager()
    st.session_state["_cookie_manager"] = cookie_manager  # reused by log_out()

    if not cookie_manager.ready():
        # Browser cookie sync hasn't completed yet - stopping here (rather
        # than falling through to the login form) lets the automatic
        # rerun the component triggers once it's synced retry this whole
        # function, this time with .ready() == True.
        st.info("Loading…")
        st.stop()

    token = cookie_manager.get(SESSION_COOKIE_NAME)
    if token:
        result = validate_session_token(token)
        if result:
            user_id, username, is_admin = result
            st.session_state.user_id = user_id
            st.session_state.username = username
            st.session_state.is_admin = is_admin
            return

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

            new_token = create_session_token(user_id)
            cookie_manager.set(
                SESSION_COOKIE_NAME,
                new_token,
                max_age=timedelta(days=SESSION_LIFETIME_DAYS),
                secure=True,
            )
            st.rerun()
        else:
            st.error("Incorrect username or password.")

    st.stop()


def log_out():
    """
    Call from a "Log out" button. Revokes the session server-side, clears
    the browser cookie, and clears the relevant session_state keys.
    """
    import streamlit as st

    cookie_manager = st.session_state.get("_cookie_manager") or _get_cookie_manager()
    if cookie_manager.ready():
        token = cookie_manager.get(SESSION_COOKIE_NAME)
        if token:
            revoke_session_token(token)
            cookie_manager.delete(SESSION_COOKIE_NAME)

    for key in ("user_id", "username", "is_admin"):
        st.session_state.pop(key, None)

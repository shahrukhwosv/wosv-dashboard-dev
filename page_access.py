"""
Page access grants: which pages a user can see in the sidebar, independent
of which stores they can see within those pages (see store_access.py for
the store-level grants - this is the same pattern, applied one level up).

Admin accounts always see every page automatically (no grants needed).
Regular users only see pages explicitly granted to them here. "Add Store"
and "Manage Users" are unaffected by this - they stay admin-only exactly
as before, regardless of what's granted here.
"""

from lightspeed_client import _get_db_connection

# (page_key, display_label) - page_key is what's stored in the database
# and used to build the sidebar in app.py; display_label is shown in the
# Manage Users editor. Keep this in sync with the st.Page(...) entries in
# app.py - add a new page here (and give it a page_key) when a new page
# is added to app.py's `page_objects` dict.
PAGE_REGISTRY = [
    ("commissions", "Commissions"),
    ("transactions", "Transactions"),
    ("touch_tell", "Touch Tell"),
    ("pace_calculator", "Pace Calculator"),
    ("category_sales", "Category Sales"),
    ("monthly_reports", "Monthly Reports"),
]
PAGE_LABELS = dict(PAGE_REGISTRY)
ALL_PAGE_KEYS = [key for key, _ in PAGE_REGISTRY]


def _require_db():
    conn = _get_db_connection()
    if conn is None:
        raise RuntimeError(
            "No database configured (DATABASE_URL is not set) - page "
            "access grants require a database."
        )
    return conn


def _ensure_page_access_table(conn):
    from auth import _ensure_users_table
    _ensure_users_table(conn)  # FK dependency - must exist first
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_page_access (
                user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                page_key TEXT NOT NULL,
                granted_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (user_id, page_key)
            )
            """
        )
    conn.commit()


def set_user_page_access(user_id, page_keys):
    """Replaces a user's entire set of accessible pages with exactly
    page_keys (a list/set). Same grant/revoke-diff pattern as
    store_access.set_user_access - grants whatever's newly checked,
    revokes whatever's newly unchecked."""
    conn = _require_db()
    try:
        _ensure_page_access_table(conn)
        page_keys = set(page_keys)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT page_key FROM user_page_access WHERE user_id = %s",
                (user_id,),
            )
            current = {row[0] for row in cur.fetchall()}

            to_add = page_keys - current
            to_remove = current - page_keys

            for key in to_add:
                cur.execute(
                    "INSERT INTO user_page_access (user_id, page_key) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (user_id, key),
                )
            for key in to_remove:
                cur.execute(
                    "DELETE FROM user_page_access WHERE user_id = %s AND page_key = %s",
                    (user_id, key),
                )
        conn.commit()
    finally:
        conn.close()


def get_accessible_pages(user_id):
    """Returns the set of page_keys this user has been granted access to."""
    conn = _require_db()
    try:
        _ensure_page_access_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT page_key FROM user_page_access WHERE user_id = %s",
                (user_id,),
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

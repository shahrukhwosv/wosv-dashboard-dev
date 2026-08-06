"""
Store access grants: which users can see which stores, independent of who
originally connected the store via OAuth.

Admin accounts always see every connected store automatically (no grants
needed). Regular users only see stores explicitly granted to them here.
"""

from lightspeed_client import _get_db_connection


def _require_db():
    conn = _get_db_connection()
    if conn is None:
        raise RuntimeError(
            "No database configured (DATABASE_URL is not set) - store "
            "access grants require a database."
        )
    return conn


def _ensure_access_table(conn):
    from auth import _ensure_users_table
    _ensure_users_table(conn)  # FK dependency - must exist first
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_store_access (
                user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                store_key TEXT NOT NULL,
                granted_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (user_id, store_key)
            )
            """
        )
    conn.commit()


def grant_access(user_id, store_key):
    conn = _require_db()
    try:
        _ensure_access_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_store_access (user_id, store_key)
                VALUES (%s, %s)
                ON CONFLICT (user_id, store_key) DO NOTHING
                """,
                (user_id, store_key),
            )
        conn.commit()
    finally:
        conn.close()


def revoke_access(user_id, store_key):
    conn = _require_db()
    try:
        _ensure_access_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_store_access WHERE user_id = %s AND store_key = %s",
                (user_id, store_key),
            )
        conn.commit()
    finally:
        conn.close()


def set_user_access(user_id, store_keys):
    """Replaces a user's entire set of accessible stores with exactly
    store_keys (a list/set). Used by the admin's per-user access editor -
    grants whatever's newly checked, revokes whatever's newly unchecked."""
    conn = _require_db()
    try:
        _ensure_access_table(conn)
        store_keys = set(store_keys)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT store_key FROM user_store_access WHERE user_id = %s",
                (user_id,),
            )
            current = {row[0] for row in cur.fetchall()}

            to_add = store_keys - current
            to_remove = current - store_keys

            for key in to_add:
                cur.execute(
                    "INSERT INTO user_store_access (user_id, store_key) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (user_id, key),
                )
            for key in to_remove:
                cur.execute(
                    "DELETE FROM user_store_access WHERE user_id = %s AND store_key = %s",
                    (user_id, key),
                )
        conn.commit()
    finally:
        conn.close()


def get_accessible_store_keys(user_id):
    """Returns the set of store_keys this user has been granted access to."""
    conn = _require_db()
    try:
        _ensure_access_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT store_key FROM user_store_access WHERE user_id = %s",
                (user_id,),
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

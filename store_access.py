"""
Store access grants: which users can see which stores, independent of who
originally connected the store via OAuth.

Admin accounts always see every connected store automatically (no grants
needed). Regular users only see stores explicitly granted to them here.

Also holds named, admin-editable STORE LISTS that further narrow what a
given page shows, on top of (not instead of) per-user access - see
get_page_store_keys, the single entry point pages should use instead of
each re-implementing the admin/accessible-keys logic inline.
"""

import json

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


def rename_store_key_everywhere(old_key, new_key):
    """
    Renames a store_key across every table store_access.py owns: per-user
    grants (user_store_access) and the named store lists
    (store_list_config). Does NOT touch config["stores"] itself - the
    caller is responsible for that (see add_store_page.py's "Rename a
    store key" tool), since config lives in lightspeed_client.py's
    load_config/save_config, not here.

    Used when a store's data was accumulating under one key (e.g. an
    original numbered store_key like "store_13") but got reconnected
    under a different key later (e.g. a newer slug-based key like
    "norman") - rather than starting a second, disconnected history,
    this folds the newer connection's key into the one the historical
    data already uses.
    """
    conn = _require_db()
    try:
        _ensure_access_table(conn)
        _ensure_store_list_table(conn)
        with conn.cursor() as cur:
            # Drop any old_key grant that would collide with a user who
            # already separately has new_key granted, then rename the rest.
            cur.execute(
                """
                DELETE FROM user_store_access a
                USING user_store_access b
                WHERE a.store_key = %s AND b.store_key = %s AND a.user_id = b.user_id
                """,
                (old_key, new_key),
            )
            cur.execute(
                "UPDATE user_store_access SET store_key = %s WHERE store_key = %s",
                (new_key, old_key),
            )

            cur.execute("SELECT list_name, store_keys FROM store_list_config")
            list_rows = cur.fetchall()
            for list_name, keys in list_rows:
                keys_set = set(keys)
                if old_key in keys_set:
                    keys_set.discard(old_key)
                    keys_set.add(new_key)
                    cur.execute(
                        "UPDATE store_list_config SET store_keys = %s, updated_at = now() "
                        "WHERE list_name = %s",
                        (json.dumps(sorted(keys_set)), list_name),
                    )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Named, admin-editable store lists
#
# These narrow which stores a PAGE operates on, on top of (not instead
# of) the per-user access above - e.g. "only show these 11 stores on
# Commissions/Transactions/Touch Tell/Monthly Reports, regardless of how
# many stores a given user/admin can otherwise see". Edited from the
# Manage Users page.
# ----------------------------------------------------------------------

STANDARD_STORES_LIST = "standard_stores"
PACE_CALCULATOR_STORES_LIST = "pace_calculator_stores"

# The 11 original Texas stores (see touch_tell_config.json's
# store_sheet_to_key) - default for the Standard Stores list until an
# admin changes it via Manage Users.
DEFAULT_STANDARD_STORE_KEYS = [f"store_{n}" for n in range(1, 12)]


def _ensure_store_list_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS store_list_config (
                list_name TEXT PRIMARY KEY,
                store_keys JSONB NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
    conn.commit()


def get_store_list(list_name, default_keys=None):
    """
    Returns the admin-saved set of store_keys for list_name, or
    default_keys (falling back to an empty set) if nothing's been saved
    yet - e.g. before an admin has ever visited the editor for this list.
    Does NOT persist the default; only set_store_list() actually saves.
    """
    conn = _require_db()
    try:
        _ensure_store_list_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT store_keys FROM store_list_config WHERE list_name = %s",
                (list_name,),
            )
            row = cur.fetchone()
        if row:
            return set(row[0])
        return set(default_keys or [])
    finally:
        conn.close()


def set_store_list(list_name, store_keys):
    """Saves the admin-edited set of store_keys for list_name."""
    conn = _require_db()
    try:
        _ensure_store_list_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO store_list_config (list_name, store_keys, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (list_name) DO UPDATE
                    SET store_keys = EXCLUDED.store_keys, updated_at = now()
                """,
                (list_name, json.dumps(sorted(store_keys))),
            )
        conn.commit()
    finally:
        conn.close()


def get_page_store_keys(config, list_name=None, default_keys=None, require_connected=True):
    """
    The single entry point pages should use to figure out which stores to
    show/operate on - replaces the "if is_admin: ... else: ..." block that
    used to be copy-pasted into every page.

    Layers two things:
      1. Per-user access: admins see every connected store, regular users
         see only what's been granted to them (get_accessible_store_keys).
      2. Optionally, a further narrowing to one of the named store lists
         above (e.g. STANDARD_STORES_LIST) via get_store_list. Pass
         list_name=None for no further narrowing - full access, up to
         everything from step 1. This is what Category Sales uses.

    require_connected=False skips filtering step 1 down to stores that
    have a refresh_token - only the Pace Calculator needs this, to
    preserve its existing behavior of showing every store key in config
    even ones not yet fully connected.

    default_keys is only used the first time list_name's list is read,
    before an admin has ever saved one via Manage Users.
    """
    import streamlit as st

    stores = config["stores"]
    if st.session_state.get("is_admin"):
        if require_connected:
            user_keys = {k for k, v in stores.items() if v.get("refresh_token")}
        else:
            user_keys = set(stores.keys())
    else:
        accessible = get_accessible_store_keys(st.session_state.user_id)
        if require_connected:
            user_keys = {
                k for k in accessible if stores.get(k, {}).get("refresh_token")
            }
        else:
            user_keys = {k for k in accessible if k in stores}

    if list_name is None:
        return user_keys

    restricted = get_store_list(list_name, default_keys=default_keys)
    return {k for k in user_keys if k in restricted}

"""
Small database-backed snapshot store for Dashboard metrics that are too
expensive to compute live on every page load.

Mama's Sold: nightly_refresh.py computes it once a night and saves it
here; dashboard_page.py reads it back instead of making a live Lightspeed
fetch on every page view.

DoorDash sales: nightly_refresh.py saves yesterday's DoorDash orders per
Pace Calculator store; morning_brief.py reads it for the Retail section.

Structured as a generic metric_key -> JSON blob table so a future metric can reuse the same
save/load pattern without a new table.
"""
import json
from datetime import date

from lightspeed_client import _get_db_connection


def _require_db():
    conn = _get_db_connection()
    if conn is None:
        raise RuntimeError(
            "No database configured (DATABASE_URL is not set) - the "
            "Dashboard's cached metrics require a database."
        )
    return conn


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_snapshot (
                metric_key TEXT PRIMARY KEY,
                snapshot_date DATE NOT NULL,
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
    conn.commit()


def save_mama_snapshot(snapshot_date, total, quantity, failed_stores):
    """Called by nightly_refresh.py after computing Mama's Sold for one
    day across every store."""
    conn = _require_db()
    try:
        _ensure_table(conn)
        data = {"total": total, "quantity": quantity, "failed_stores": list(failed_stores)}
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dashboard_snapshot (metric_key, snapshot_date, data, updated_at)
                VALUES ('mama_sold', %s, %s, now())
                ON CONFLICT (metric_key) DO UPDATE
                    SET snapshot_date = EXCLUDED.snapshot_date,
                        data = EXCLUDED.data,
                        updated_at = now()
                """,
                (snapshot_date.isoformat(), json.dumps(data)),
            )
        conn.commit()
    finally:
        conn.close()


def load_mama_snapshot():
    """Returns (snapshot_date, total, quantity, failed_stores, updated_at),
    or all-None if nothing's been saved yet (e.g. the nightly job hasn't
    run for the first time) or no database is configured."""
    conn = _get_db_connection()
    if conn is None:
        return None, None, None, None, None
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT snapshot_date, data, updated_at FROM dashboard_snapshot "
                "WHERE metric_key = 'mama_sold'"
            )
            row = cur.fetchone()
        if not row:
            return None, None, None, None, None
        snapshot_date_val, data, updated_at = row
        snapshot_date_obj = snapshot_date_val if isinstance(snapshot_date_val, date) else date.fromisoformat(str(snapshot_date_val))
        return snapshot_date_obj, data["total"], data["quantity"], data.get("failed_stores", []), updated_at
    finally:
        conn.close()


def save_doordash_snapshot(snapshot_date, stores, failed_stores):
    """Called by nightly_refresh.py with yesterday's DoorDash sales at the
    Pace Calculator stores. stores: [{"name", "orders", "items", "sales"}]
    for every checked store (including ones with 0 orders - the morning
    brief hides those)."""
    conn = _require_db()
    try:
        _ensure_table(conn)
        data = {"stores": list(stores), "failed_stores": list(failed_stores)}
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dashboard_snapshot (metric_key, snapshot_date, data, updated_at)
                VALUES ('doordash_sales', %s, %s, now())
                ON CONFLICT (metric_key) DO UPDATE
                    SET snapshot_date = EXCLUDED.snapshot_date,
                        data = EXCLUDED.data,
                        updated_at = now()
                """,
                (snapshot_date.isoformat(), json.dumps(data)),
            )
        conn.commit()
    finally:
        conn.close()


def load_doordash_snapshot():
    """Returns (snapshot_date, stores, failed_stores), or all-None if the
    nightly job hasn't saved one yet or no database is configured."""
    conn = _get_db_connection()
    if conn is None:
        return None, None, None
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT snapshot_date, data FROM dashboard_snapshot "
                "WHERE metric_key = 'doordash_sales'"
            )
            row = cur.fetchone()
        if not row:
            return None, None, None
        snapshot_date_val, data = row
        snapshot_date_obj = snapshot_date_val if isinstance(snapshot_date_val, date) else date.fromisoformat(str(snapshot_date_val))
        return snapshot_date_obj, data.get("stores", []), data.get("failed_stores", [])
    finally:
        conn.close()

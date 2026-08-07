"""
Stores one row per store per day: total sales, total units, total sale
count. Populated by sync_daily_sales.py (run nightly via a Railway Cron
Job, and once manually for the initial historical backfill).

The dashboard reads from this table instead of hitting the Lightspeed API
live - turns a multi-minute live pull (65 stores x 6 months) into an
instant database query, and survives app restarts/redeploys since it's
real persistent data, not an in-memory cache.
"""

from datetime import date, timedelta
import calendar

from lightspeed_client import _get_db_connection


def _require_db():
    conn = _get_db_connection()
    if conn is None:
        raise RuntimeError(
            "No database configured (DATABASE_URL is not set) - daily "
            "sales summaries require a database."
        )
    return conn


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_store_sales (
                store_key TEXT NOT NULL,
                sale_date DATE NOT NULL,
                total_sales NUMERIC NOT NULL DEFAULT 0,
                total_units NUMERIC NOT NULL DEFAULT 0,
                total_sale_count INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (store_key, sale_date)
            )
            """
        )
    conn.commit()


def upsert_daily_sales(store_key, sale_date, total_sales, total_units, total_sale_count):
    conn = _require_db()
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO daily_store_sales
                    (store_key, sale_date, total_sales, total_units, total_sale_count, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (store_key, sale_date) DO UPDATE SET
                    total_sales = EXCLUDED.total_sales,
                    total_units = EXCLUDED.total_units,
                    total_sale_count = EXCLUDED.total_sale_count,
                    updated_at = now()
                """,
                (store_key, sale_date, total_sales, total_units, total_sale_count),
            )
        conn.commit()
    finally:
        conn.close()


def get_daily_metrics(store_keys, target_date):
    """Returns (total_sales, total_units, total_sale_count) summed across
    store_keys for target_date, reading from stored data - no API calls."""
    if not store_keys:
        return 0.0, 0.0, 0

    conn = _require_db()
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(total_sales), 0),
                       COALESCE(SUM(total_units), 0),
                       COALESCE(SUM(total_sale_count), 0)
                FROM daily_store_sales
                WHERE store_key = ANY(%s) AND sale_date = %s
                """,
                (list(store_keys), target_date),
            )
            total_sales, total_units, total_sale_count = cur.fetchone()
            return float(total_sales), float(total_units), int(total_sale_count)
    finally:
        conn.close()


def get_monthly_trend(store_keys, months_back, today):
    """
    Returns [(store_key, month_label, total_sales), ...] for the last
    months_back calendar months up to and including the current one,
    summed from stored daily data - no API calls.
    """
    if not store_keys:
        return [], []

    year, month = today.year, today.month
    for _ in range(months_back - 1):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    earliest_start = date(year, month, 1)

    # Build the expected month labels in chronological order, even for
    # months with no data yet, so the chart's x-axis doesn't have gaps.
    month_order = []
    y, m = year, month
    for _ in range(months_back):
        month_order.append(date(y, m, 1).strftime("%b %Y"))
        m += 1
        if m == 13:
            m = 1
            y += 1

    conn = _require_db()
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT store_key,
                       date_trunc('month', sale_date) AS month_start,
                       SUM(total_sales) AS total
                FROM daily_store_sales
                WHERE store_key = ANY(%s) AND sale_date >= %s
                GROUP BY store_key, month_start
                ORDER BY month_start
                """,
                (list(store_keys), earliest_start),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    records = [
        (store_key, month_start.strftime("%b %Y"), float(total))
        for store_key, month_start, total in rows
    ]
    return records, month_order

"""
Purchase order fetching for the Purchase Order Status page.

Unlike lightspeed_po.py's fetch_purchase_orders_for_vendor (which is
scoped to one specific vendor, for Touch Tell matching), this pulls every
PO regardless of vendor, across all stages, so a status page can show
what's sitting in Open/Ordered/Check In right now.

Reuses lightspeed_po.py's confirmed stage-derivation rule (derive_stage)
rather than re-deriving it - see that module's docstring for how the 4
stages (Open/Ordered/Check In/Finished) are confirmed to map onto the 3
underlying fields (complete, orderedDate, receivedDate).

FIELD CONFIRMATION STATUS:
  - complete, orderedDate, receivedDate, refNum, totalCost, vendorID:
    confirmed against real data (see lightspeed_po.py).
  - createTime: already used successfully as a filter in
    fetch_purchase_orders_for_vendor (production Touch Tell code), so
    trusted here too, for both filtering and the "days since created"
    column.
  - notes: CONFIRMED the earlier guesses (top-level "notes"/"note"
    fields) were wrong - a real Order record has no note text on it
    directly, only a noteID pointing at a separate related record (same
    pattern this API uses for categoryID/vendorID/discountID elsewhere).
    Now requests load_relations=["Note"] and pulls text out of that
    relation instead. The exact field name for the note's text WITHIN
    that relation still isn't confirmed (tries "note"/"text"/"memo"/
    "body") - if this is still blank after deploying, run
    inspect_po_sample.py again (now also loads the Note relation) against
    a PO you know has a note on it and paste back the "Note" section so
    we can pin down the exact key.
"""
from datetime import date, datetime, timedelta, timezone
import json

from lightspeed_client import fetch_all, _get_db_connection
from lightspeed_po import derive_stage


def fetch_vendor_lookup(config, store_key):
    """Returns {vendorID: name} for one store."""
    vendors = fetch_all(config, store_key, "Vendor.json", params={"limit": 200})
    return {str(v.get("vendorID", "")): v.get("name", "") for v in vendors}


def _extract_note_text(po):
    """
    Pulls the note's text out of the loaded Note relation (see module
    docstring - Order records only carry a noteID, not the text itself).
    Defensive about both the relation's shape (a single dict, or a list
    if an order can somehow have more than one) and which key inside it
    actually holds the text (not confirmed - tries a few plausible
    names). Returns "" if there's no note relation at all, or none of
    the guessed keys have text in them.
    """
    note_rel = po.get("Note")
    if not note_rel:
        return ""
    entries = note_rel if isinstance(note_rel, list) else [note_rel]
    texts = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = entry.get("note") or entry.get("text") or entry.get("memo") or entry.get("body") or ""
        text = str(text).strip()
        if text:
            texts.append(text)
    return " | ".join(texts)


def fetch_purchase_order_status(config, store_key, months_back=6):
    """
    Pulls every PO for one store (any vendor, any stage) created in the
    last `months_back` months.

    Returns a list of normalized dicts:
      {
        "store": store_key,
        "po_id": ...,
        "reference_number": str,        # Lightspeed's own PO number
        "vendor_id": str,
        "notes": str,                   # vendor's order/invoice number, per DMs' new process
        "stage": one of lightspeed_po.STAGE_ORDER,
        "create_time": str or None,     # raw ISO string
        "ordered_date": str or None,
        "received_date": str or None,
        "total": float,
      }
    """
    since = datetime.now(timezone.utc) - timedelta(days=30 * months_back)
    params = [
        ("limit", 100),
        ("createTime", f">,{since.isoformat(timespec='seconds')}"),
        ("load_relations", '["Note"]'),
    ]
    orders = fetch_all(config, store_key, "Order.json", params=params, record_key="Order")

    results = []
    for po in orders:
        results.append({
            "store": store_key,
            "po_id": po.get("orderID"),
            "reference_number": str(po.get("refNum", "") or "").strip(),
            "vendor_id": str(po.get("vendorID", "") or ""),
            "notes": _extract_note_text(po),
            "stage": derive_stage(po),
            "create_time": po.get("createTime") or None,
            "ordered_date": po.get("orderedDate") or None,
            "received_date": po.get("receivedDate") or None,
            "total": float(po.get("totalCost", 0) or 0),
        })
    return results


# ----------------------------------------------------------------------
# Snapshot persistence
#
# The Purchase Order Status page's table is expensive to build (one
# Lightspeed fetch per store), so rather than re-fetching on every page
# view, the last-loaded table is saved here and shown until "Load
# purchase orders" is clicked again - deliberately persisted to the
# database (not just st.session_state) so it survives a logout/login or
# opening the page in a fresh session, not just clicks within one
# browser tab. There's only ever one saved snapshot (not one per user) -
# whoever last clicked "Load purchase orders" is what everyone sees.
# ----------------------------------------------------------------------

def _ensure_snapshot_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS po_status_snapshot (
                id INTEGER PRIMARY KEY DEFAULT 1,
                rows_json JSONB NOT NULL,
                loaded_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT po_status_snapshot_single_row CHECK (id = 1)
            )
            """
        )
    conn.commit()


def save_po_status_snapshot(rows):
    """Persists the last-loaded table. Silently does nothing if no
    database is configured - the page just won't have anything to fall
    back on between sessions in that case, same as before this existed."""
    conn = _get_db_connection()
    if conn is None:
        return
    try:
        _ensure_snapshot_table(conn)
        serializable_rows = []
        for row in rows:
            row_copy = dict(row)
            for key in ("Created", "Ordered"):
                if isinstance(row_copy.get(key), date):
                    row_copy[key] = row_copy[key].isoformat()
            serializable_rows.append(row_copy)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO po_status_snapshot (id, rows_json, loaded_at)
                VALUES (1, %s, now())
                ON CONFLICT (id) DO UPDATE
                    SET rows_json = EXCLUDED.rows_json, loaded_at = now()
                """,
                (json.dumps(serializable_rows),),
            )
        conn.commit()
    finally:
        conn.close()


def load_po_status_snapshot():
    """Returns (rows, loaded_at) from the last save_po_status_snapshot()
    call, or (None, None) if nothing's been saved yet (or no database is
    configured)."""
    conn = _get_db_connection()
    if conn is None:
        return None, None
    try:
        _ensure_snapshot_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT rows_json, loaded_at FROM po_status_snapshot WHERE id = 1")
            row = cur.fetchone()
        if not row:
            return None, None
        rows_json, loaded_at = row
        rows = []
        for r in rows_json:
            r = dict(r)
            for key in ("Created", "Ordered"):
                if r.get(key):
                    try:
                        r[key] = date.fromisoformat(r[key])
                    except ValueError:
                        pass
            rows.append(r)
        return rows, loaded_at
    finally:
        conn.close()

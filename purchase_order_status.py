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
  - notes: NOT independently confirmed against a real API response.
    Included because that's where district managers are being asked to
    record the vendor's own order/invoice number (Lightspeed's own
    reference number doesn't always match it) - if this comes back
    empty even for POs you know have a note, dump one raw Order with
    inspect_sample.py and we'll check the actual field name.
"""
from datetime import datetime, timedelta, timezone

from lightspeed_client import fetch_all
from lightspeed_po import derive_stage


def fetch_vendor_lookup(config, store_key):
    """Returns {vendorID: name} for one store."""
    vendors = fetch_all(config, store_key, "Vendor.json", params={"limit": 200})
    return {str(v.get("vendorID", "")): v.get("name", "") for v in vendors}


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
    ]
    orders = fetch_all(config, store_key, "Order.json", params=params, record_key="Order")

    results = []
    for po in orders:
        results.append({
            "store": store_key,
            "po_id": po.get("orderID"),
            "reference_number": str(po.get("refNum", "") or "").strip(),
            "vendor_id": str(po.get("vendorID", "") or ""),
            "notes": str(po.get("notes", "") or "").strip(),
            "stage": derive_stage(po),
            "create_time": po.get("createTime") or None,
            "ordered_date": po.get("orderedDate") or None,
            "received_date": po.get("receivedDate") or None,
            "total": float(po.get("totalCost", 0) or 0),
        })
    return results

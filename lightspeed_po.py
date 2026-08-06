"""
Purchase Order fetching for the Touch Tell matching tool.

NOTE ON THE ENDPOINT NAME:
Lightspeed's actual API resource for purchase orders is called "Order"
(not "PurchaseOrder" - that was a wrong first guess and returned a 404).
Confirmed against developers.lightspeedhq.com.

NOTE ON STAGE (CONFIRMED FROM REAL DATA):
There is no single "status" field on an Order record. The 4 stages shown
in the Lightspeed UI (Open / Ordered / Check In / Finished) are derived
from 3 fields: complete, orderedDate, receivedDate. Confirmed against 4
real orders, one of each stage:

  complete=true                              -> Finished
  complete=false, has receivedDate           -> Check In
  complete=false, has orderedDate, no receivedDate -> Ordered
  complete=false, no orderedDate             -> Open

Reference number field: refNum (confirmed).
Total field: totalCost (confirmed).

NOTE ON FILTERING (fixed after a real bug):
The first version of this function pulled EVERY order on the account
(all vendors, all history) and filtered by vendor name client-side after
fetching each page, with a 5,000-order safety cutoff. On an account with
years of order history across many vendors, that cutoff can be hit before
pagination ever reaches this year's Touch Tell orders - so recent, very
real invoices were coming back as "no matching PO found" even though they
existed. Fixed by filtering at the API level: look up Touch Tell's
vendorID once, then ask Lightspeed for only that vendor's orders within a
recent date window.
"""
from datetime import datetime, timedelta, timezone

from lightspeed_client import api_get, api_get_full_url

# --- Field name constants (confirmed against real API responses) ---
FIELD_REFERENCE_NUM = "refNum"
FIELD_TOTAL = "totalCost"

STAGE_ORDER = ["open", "ordered", "check_in", "finished"]
READY_STAGES = {"ordered", "check_in", "finished"}


def _derive_stage(order):
    """Confirmed rule - see module docstring."""
    complete = str(order.get("complete", "false")).strip().lower() == "true"
    if complete:
        return "finished"
    if str(order.get("receivedDate", "") or "").strip():
        return "check_in"
    if str(order.get("orderedDate", "") or "").strip():
        return "ordered"
    return "open"


def _find_vendor_id(config, store_key, vendor_name):
    """Looks up the vendorID for a vendor by name (case-insensitive)."""
    data = api_get(config, store_key, "Vendor.json", params={"limit": 200})
    raw = data.get("Vendor", [])
    if isinstance(raw, dict):
        raw = [raw]
    for vendor in raw:
        if str(vendor.get("name", "")).strip().casefold() == vendor_name.strip().casefold():
            return vendor.get("vendorID")
    return None


def fetch_purchase_orders_for_vendor(config, store_key, vendor_name, months_back=18, max_pages=50):
    """
    Pulls Purchase Orders for one store, filtered at the API level to the
    given vendor (by vendorID, looked up from the name) and to orders
    created in the last `months_back` months. See module docstring for why
    server-side filtering replaced the original client-side approach.

    Returns a list of normalized dicts:
      {
        "po_id": ...,
        "reference_number": str,
        "stage": one of STAGE_ORDER,
        "total": float,
      }
    """
    vendor_id = _find_vendor_id(config, store_key, vendor_name)
    if vendor_id is None:
        print(f"[{store_key}] No vendor named '{vendor_name}' found on this account.")
        return []

    since = datetime.now(timezone.utc) - timedelta(days=30 * months_back)
    results = []
    params = {
        "limit": 100,
        "vendorID": vendor_id,
        "createTime": f">,{since.isoformat(timespec='seconds')}",
    }

    print(f"[{store_key}] Requesting page 1 of {vendor_name} purchase orders...")
    data = api_get(config, store_key, "Order.json", params=params)

    page = 1
    seen_urls = set()
    while True:
        raw = data.get("Order", [])
        if isinstance(raw, dict):
            raw = [raw]

        for po in raw:
            stage = _derive_stage(po)
            results.append({
                "po_id": po.get("orderID"),
                "reference_number": str(po.get(FIELD_REFERENCE_NUM, "") or "").strip(),
                "stage": stage,
                "total": float(po.get(FIELD_TOTAL, 0) or 0),
            })

        next_url = (data.get("@attributes", {}) or {}).get("next")
        if not next_url or next_url in seen_urls:
            break
        if page >= max_pages:
            print(f"[{store_key}] Hit the {max_pages}-page safety limit, stopping early.")
            break
        seen_urls.add(next_url)
        page += 1
        print(f"[{store_key}] Requesting page {page}...")
        data = api_get_full_url(config, store_key, next_url)

    print(f"[{store_key}] Done. {len(results)} {vendor_name} purchase order(s) found.")
    return results

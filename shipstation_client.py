"""
Minimal ShipStation API (v1) client - just enough to look up what a
shipping label cost for a given order.

AUTH: set SHIPSTATION_API_KEY and SHIPSTATION_API_SECRET as environment
variables (Railway: on both the web app and the nightly cron service).
Get them from ShipStation > Settings > Account > API Settings.

ShipStation allows 40 API requests per minute; when that's exceeded it
answers 429 with an X-Rate-Limit-Reset header (seconds to wait), which
this honors.
"""
import os
import time

import requests

SHIPSTATION_BASE_URL = "https://ssapi.shipstation.com"


def is_configured():
    return bool(os.getenv("SHIPSTATION_API_KEY") and os.getenv("SHIPSTATION_API_SECRET"))


def _get(path, params):
    auth = (os.getenv("SHIPSTATION_API_KEY"), os.getenv("SHIPSTATION_API_SECRET"))
    for _ in range(3):
        resp = requests.get(f"{SHIPSTATION_BASE_URL}{path}", params=params, auth=auth, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("X-Rate-Limit-Reset", "10") or 10)
            time.sleep(min(max(wait, 1), 60) + 1)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def label_cost(shipstation_order_id=None, order_number=None):
    """
    Total label cost (shipmentCost + insuranceCost) of every non-voided
    shipment on a ShipStation order, or None if no shipment was found.

    Looks the order up by ShipStation's own orderId first (the ERP stores
    it on each invoice's shipment record as channelOrderId), falling back
    to the order number (= ERP invoice number) if that finds nothing.
    """
    shipments = []
    if shipstation_order_id:
        shipments = (_get("/shipments", {"orderId": shipstation_order_id, "pageSize": 100}) or {}).get("shipments") or []
    if not shipments and order_number:
        found = (_get("/shipments", {"orderNumber": order_number, "pageSize": 100}) or {}).get("shipments") or []
        shipments = [s for s in found if str(s.get("orderNumber")) == str(order_number)]

    live = [s for s in shipments if not s.get("voided")]
    if not live:
        return None
    return round(sum((s.get("shipmentCost") or 0) + (s.get("insuranceCost") or 0) for s in live), 2)

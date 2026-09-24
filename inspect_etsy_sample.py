"""
Prints raw Etsy data for the last few days - receipts, their payment
records, and every payment-account ledger entry with how it was matched -
so the fee matching in etsy_orders.py can be checked against Etsy's own
numbers (Shop Manager > Finances > Payment account). Read-only.

Run (with the same env vars as the app):  python inspect_etsy_sample.py [days]
"""
import json
import sys
from datetime import datetime, timedelta, timezone

from etsy_client import EtsyClient
from etsy_orders import _is_fee, assign_fees

days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
etsy = EtsyClient()
end = datetime.now(timezone.utc)
start = end - timedelta(days=days)

receipts = etsy.receipts(start, end)
entries = etsy.ledger_entries(start - timedelta(days=1), end)
per_order, unmatched = assign_fees(receipts, entries)

if receipts:
    print("=== FIRST RECEIPT (raw) ===")
    print(json.dumps(receipts[0], indent=2)[:6000])
    print("=== ITS PAYMENTS ===")
    print(json.dumps(etsy.receipt_payments(receipts[0]["receipt_id"]), indent=2)[:3000])

print(f"\n=== LEDGER ENTRIES ({len(entries)}) ===")
matched = {id(e): rid for rid, es in per_order.items() for e in es}
for e in entries:
    how = matched.get(id(e)) or ("OTHER FEE" if _is_fee(e) else "not a fee")
    print(f"{e.get('ledger_type'):<28} {e.get('reference_type') or '':<14} {e.get('reference_id') or '':<14} "
          f"{(e.get('amount') or 0) / 100:>9.2f}  -> {how}   {e.get('description') or ''}")

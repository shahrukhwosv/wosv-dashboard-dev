"""
Run this to see exactly what one real Purchase Order (Lightspeed's
"Order" resource) looks like - specifically to nail down which field
actually holds the vendor's order/invoice number that district managers
type into the PO's General Notes field, since purchase_order_status.py
currently guesses between "notes" and "note" (see that file's docstring)
rather than using a confirmed field name.

Usage:
  python inspect_po_sample.py store_1
      Dumps up to 5 recent POs - look through them for the one you know
      has a note on it, and check which key actually holds that text.

  python inspect_po_sample.py store_1 PO-1234
      Dumps just the one PO with that refNum (use a PO you already know
      has a note on it, for a faster/more direct check).

Paste whichever raw JSON has the note back to Claude and we'll lock in
the correct field name in purchase_order_status.py.
"""
import sys
import json
from datetime import datetime, timedelta, timezone

from lightspeed_client import load_config, fetch_all


def main():
    if len(sys.argv) not in (2, 3):
        print("Usage: python inspect_po_sample.py store_1 [PO-refNum]")
        sys.exit(1)

    store_key = sys.argv[1]
    target_ref = sys.argv[2] if len(sys.argv) == 3 else None
    config = load_config()

    since = datetime.now(timezone.utc) - timedelta(days=180)
    params = [("limit", 100), ("createTime", f">,{since.isoformat(timespec='seconds')}")]
    orders = fetch_all(config, store_key, "Order.json", params=params, record_key="Order")

    if not orders:
        print(f"No purchase orders found for {store_key} in the last 180 days.")
        return

    if target_ref:
        matches = [o for o in orders if str(o.get("refNum", "")).strip() == target_ref]
        if not matches:
            print(f"No PO found with refNum '{target_ref}' in the last 180 days. Showing the first PO found instead.\n")
            matches = orders[:1]
        print(json.dumps(matches[0], indent=2))
        return

    print(f"{len(orders)} PO(s) found - showing up to 5. Look for the one you know has a note, "
          f"and check which key holds that text.\n")
    for order in orders[:5]:
        print(json.dumps(order, indent=2))
        print("---")


if __name__ == "__main__":
    main()

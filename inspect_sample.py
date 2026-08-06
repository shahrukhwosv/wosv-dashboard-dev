"""
Run this AFTER connecting a store with oauth_setup.py to see exactly what one
real sale record looks like, especially how discounts/promos are represented.

Usage: python inspect_sample.py store_1

Paste the output back to Claude and we'll tighten up the promo-matching logic
in lightspeed_client.normalize_sale() to match your account's exact field
names.
"""

import sys
import json
from datetime import date, timedelta
from lightspeed_client import load_config, fetch_sales

def main():
    if len(sys.argv) != 2:
        print("Usage: python inspect_sample.py store_1")
        sys.exit(1)

    store_key = sys.argv[1]
    config = load_config()

    end = date.today()
    start = end - timedelta(days=30)

    sales = fetch_sales(config, store_key, start, end)
    if not sales:
        print(f"No sales found for {store_key} in the last 30 days.")
        return

    # find one sale that has a discount, if possible, else just show the first
    sample = sales[0]
    for s in sales:
        lines = s.get("SaleLines", {}).get("SaleLine", [])
        if isinstance(lines, dict):
            lines = [lines]
        if any(float(l.get("discountAmount", 0) or 0) != 0 for l in lines):
            sample = s
            break

    print(json.dumps(sample, indent=2))


if __name__ == "__main__":
    main()

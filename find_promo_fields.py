"""
Finds exactly where your promo names show up in real Lightspeed sale data,
so we can confirm (or fix) the matching logic in lightspeed_client.py.

Usage: python find_promo_fields.py store_1

It pulls the last 60 days of sales for that store, searches the ENTIRE raw
JSON text of every sale for each promo name configured in rules_config.json,
and prints out the full sale record for the first couple of matches it finds
per promo - so we can see exactly which field holds the name.

If it finds NO matches for a promo name, that tells us something too (wrong
spelling, promo represented differently than a plain text field, etc) -
paste that back to Claude either way.
"""

import sys
import json
from datetime import date, timedelta

from lightspeed_client import load_config, fetch_sales
from commission_engine import load_rules


def get_all_promo_names():
    rules = load_rules()
    names = []
    for r in rules:
        if r["type"] == "promo_applied":
            n = r["promo_name_match"]
            names.extend([n] if isinstance(n, str) else n)
    return names


def main():
    if len(sys.argv) != 2:
        print("Usage: python find_promo_fields.py store_1")
        sys.exit(1)

    store_key = sys.argv[1]
    config = load_config()
    promo_names = get_all_promo_names()

    print(f"Looking for these promo names: {promo_names}\n")
    print("Pulling last 60 days of sales (this may take a minute)...\n")

    end = date.today()
    start = end - timedelta(days=60)
    sales = fetch_sales(config, store_key, start, end)

    print(f"\nSearched {len(sales)} sales.\n")

    for promo_name in promo_names:
        matches = [s for s in sales if promo_name.lower() in json.dumps(s).lower()]
        print(f"=== '{promo_name}': {len(matches)} sale(s) contain this text ===")
        if matches:
            print("Here's the first matching sale's full data - look for which")
            print("field actually holds the promo name:\n")
            print(json.dumps(matches[0], indent=2))
        else:
            print("No matches found anywhere in the raw data for this sale set.")
            print("Double check spelling/capitalization matches rules_config.json exactly,")
            print("or the promo might not be represented as a plain product/discount name.")
        print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    main()

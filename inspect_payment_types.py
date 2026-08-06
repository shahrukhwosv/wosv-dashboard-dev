"""
Finds out exactly how Lightspeed represents payment type (cash vs card) on a
sale, so the reconciliation tool can correctly filter to card-only sales.

Usage: python inspect_payment_types.py store_1

Pulls the last 7 days of sales and prints the full raw data for a couple of
sales, so we can see the SalePayments structure. Paste the output back to
Claude so we can confirm/fix the tender-type detection logic in
lightspeed_client.py's fetch_card_sales() function.
"""

import sys
import json
from datetime import date, timedelta

from lightspeed_client import load_config, api_get


def main():
    if len(sys.argv) != 2:
        print("Usage: python inspect_payment_types.py store_1")
        sys.exit(1)

    store_key = sys.argv[1]
    config = load_config()

    end = date.today()
    start = end - timedelta(days=7)

    params = [
        ("limit", 5),
        ("completed", "true"),
        ("timeStamp", f"><,{start.isoformat()}T00:00:00+00:00,{end.isoformat()}T23:59:59+00:00"),
        ("load_relations", '["SaleLines","SalePayments","SalePayments.PaymentType"]'),
    ]
    data = api_get(config, store_key, "Sale.json", params=params)
    sales = data.get("Sale", [])
    if isinstance(sales, dict):
        sales = [sales]

    if not sales:
        print("No sales found in the last 7 days to inspect. Try again after a sale happens.")
        return

    print(f"Showing {min(2, len(sales))} sample sale(s):\n")
    for s in sales[:2]:
        print(json.dumps(s, indent=2))
        print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    main()

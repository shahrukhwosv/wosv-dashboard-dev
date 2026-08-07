"""
Pulls sales from Lightspeed for every connected store and stores daily
summaries (total sales, units, sale count) in the database.

Three ways to run this:

  Nightly (via Railway Cron Job, no arguments):
      python sync_daily_sales.py
  Syncs just YESTERDAY for every store. This is what should run
  automatically every night.

  One-time backfill from a specific date (run manually via Railway's
  Console tab):
      python sync_daily_sales.py --since 2026-01-01
  Syncs every day from that date through yesterday, for every store.

  One-time backfill for the last N days:
      python sync_daily_sales.py --backfill-days 90

  Both backfill modes can take a while for many stores/days - progress is
  printed as it goes, and it's safe to re-run or interrupt/resume since
  each day is saved as it completes (upsert, not all-or-nothing).
"""

import sys
from datetime import date, datetime, timedelta

from lightspeed_client import load_config, fetch_sales
from sales_summary import upsert_daily_sales


def _sale_units(sale):
    lines = sale.get("SaleLines", {}).get("SaleLine", [])
    if isinstance(lines, dict):
        lines = [lines]
    return sum(abs(float(line.get("unitQuantity", 1) or 1)) for line in lines)


def sync_one_day(config, store_key, target_date):
    raw_sales = fetch_sales(config, store_key, target_date, target_date)
    total_sales = 0.0
    total_units = 0.0
    for sale in raw_sales:
        total_sales += float(sale.get("total", sale.get("calcTotal", 0)) or 0)
        total_units += _sale_units(sale)
    upsert_daily_sales(store_key, target_date, total_sales, total_units, len(raw_sales))
    return total_sales, len(raw_sales)


def main():
    dates_to_sync = None

    if len(sys.argv) == 1:
        dates_to_sync = [date.today() - timedelta(days=1)]  # nightly default: just yesterday
    elif len(sys.argv) == 3 and sys.argv[1] == "--backfill-days":
        backfill_days = int(sys.argv[2])
        dates_to_sync = [
            date.today() - timedelta(days=offset)
            for offset in range(1, backfill_days + 1)
        ]
        dates_to_sync.reverse()  # oldest first, easier to watch progress climb toward today
    elif len(sys.argv) == 3 and sys.argv[1] == "--since":
        start_date = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
        yesterday = date.today() - timedelta(days=1)
        if start_date > yesterday:
            print(f"--since {start_date} is in the future (or today) - nothing to sync.")
            sys.exit(1)
        dates_to_sync = []
        d = start_date
        while d <= yesterday:
            dates_to_sync.append(d)
            d += timedelta(days=1)
    else:
        print("Usage: python sync_daily_sales.py [--backfill-days N | --since YYYY-MM-DD]")
        sys.exit(1)

    config = load_config()
    store_keys = [
        key for key, val in config["stores"].items() if val.get("refresh_token")
    ]

    if not store_keys:
        print("No connected stores found.")
        return

    total_days = len(dates_to_sync)
    print(f"Syncing {total_days} day(s) x {len(store_keys)} store(s)...\n")

    for day_num, target_date in enumerate(dates_to_sync, start=1):
        print(f"--- Day {day_num}/{total_days}: {target_date} ---")
        for store_key in store_keys:
            store_name = config["stores"][store_key].get("name", store_key)
            try:
                total_sales, sale_count = sync_one_day(config, store_key, target_date)
                print(f"  {store_name}: ${total_sales:,.2f} ({sale_count} sales)")
            except Exception as e:
                print(f"  {store_name}: FAILED - {e}")

    print("\n✅ Sync complete.")


if __name__ == "__main__":
    main()

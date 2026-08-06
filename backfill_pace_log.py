"""
One-time backfill for the daily sales pace log.

Run this manually, once, from your terminal (NOT from inside the Streamlit
app - pulling a full year of sales for every store is too slow for a single
page request and will likely time out on Railway):

    python backfill_pace_log.py

To backfill just one store (useful for testing before running the full
set), pass its store key from stores_config.json as an argument:

    python backfill_pace_log.py princeton

After this initial run, the "Refresh sales log" button on the Pace
Calculator page only has to fetch the small number of days since the last
update (normally just 1), so it stays fast.

Re-running this script later is safe - it only fetches days that aren't
already in the log.
"""
import sys

from lightspeed_client import load_config
from sales_pace import update_daily_log

if __name__ == "__main__":
    config = load_config()

    if len(sys.argv) > 1:
        store_keys = sys.argv[1:]
        unknown = [k for k in store_keys if k not in config["stores"]]
        if unknown:
            print(f"Unknown store key(s): {', '.join(unknown)}")
            print(f"Available store keys: {', '.join(config['stores'].keys())}")
            sys.exit(1)
    else:
        store_keys = list(config["stores"].keys())

    print(f"Backfilling daily sales log for: {', '.join(store_keys)}")
    added = update_daily_log(config, store_keys)
    print(f"Done. Added {added} new day(s) of data.")

"""
Nightly refresh script - run once a night via a Railway Cron Job (a
SEPARATE service from your main Streamlit app), not from inside the app
itself. Streamlit has no built-in scheduler, and a background thread
started inside the web app is unreliable on a platform that can
restart/redeploy the service at any time - a real cron-triggered script
that runs to completion and exits is the correct way to do this
reliably.

WHAT THIS DOES (and why nothing else needs to change to pick it up):
  1. Refreshes the Pace Calculator's Google Sheet - the same thing
     clicking "Fetch missing days from Lightspeed" does today. Since
     both the Pace Calculator page AND the Dashboard's Total Sales/
     Highest Store/Lowest Store/MTD Pace metrics already just READ that
     sheet (sales_pace.read_daily_log()) rather than fetching from
     Lightspeed themselves, running this nightly is enough on its own to
     keep all of those numbers current every morning - no other code
     needed to change for them.
  2. Computes Mama's Sold for yesterday across every store and saves it
     to the database (dashboard_data.save_mama_snapshot) - this ONE
     metric previously required a live Lightspeed fetch on every
     Dashboard page load (see dashboard_page.py's git history), which is
     exactly what this script now does once, overnight, instead.

DEPLOYING THIS AS A RAILWAY CRON JOB:
  1. In your Railway project, add a new service from this SAME GitHub
     repo (it becomes a second service alongside your main web app,
     sharing the same codebase - you don't duplicate anything).
  2. Set that service's Start Command to:
         python nightly_refresh.py
  3. In that service's Settings > Cron Schedule, enter a crontab
     expression. Railway evaluates cron schedules in UTC, and Central
     time shifts between UTC-6 (CST, winter) and UTC-5 (CDT, summer) -
     there's no single UTC time that's always 3:00 AM Central
     year-round, so pick one and expect to nudge it by an hour twice a
     year at daylight saving changes:
         0 9 * * *   -> 3:00 AM CST (roughly Nov-Mar)
         0 8 * * *   -> 3:00 AM CDT (roughly Mar-Nov)
  4. Give this service the SAME environment variables as your main app -
     DATABASE_URL, STORES_CONFIG_JSON, GOOGLE_SERVICE_ACCOUNT_JSON, and
     PACE_LOG_SHEET_ID at minimum. Railway lets you reference/share
     variables across services in the same project instead of retyping
     them.
  5. This script exits on its own when done, which Railway's cron jobs
     require - if a previous run is still "Active" when the next
     scheduled time comes around, Railway skips that run rather than
     overlapping them.
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

import lightspeed_client as ls
from lightspeed_client import load_config
from sales_pace import update_daily_log, store_local_today
from store_access import get_store_list, PACE_CALCULATOR_STORES_LIST
from dashboard_data import save_mama_snapshot
from topshelf_invoices import nightly_sync as refresh_topshelf_invoices


# NOTE: this script runs outside Streamlit (no logged-in user, no
# st.session_state), so it can't use store_access.get_page_store_keys() -
# that function looks up the current user's permissions and would crash
# here. Instead it behaves like an admin: every store in config, narrowed
# by the same admin-saved store lists the pages use.


def refresh_pace_log(config):
    all_keys = list(config["stores"].keys())
    restricted = get_store_list(PACE_CALCULATOR_STORES_LIST, default_keys=all_keys)
    store_keys = [k for k in all_keys if k in restricted]
    print(f"[pace log] Refreshing {len(store_keys)} store(s)...")
    added = update_daily_log(config, store_keys)
    print(f"[pace log] Done - added {added} new day(s) of data.")


def refresh_mama_sold(config):
    # Every connected store (has a refresh_token), same as an admin on Category Sales
    store_keys = sorted(k for k, v in config["stores"].items() if v.get("refresh_token"))
    snapshot_date = store_local_today() - timedelta(days=1)
    print(f"[mama's sold] Fetching {snapshot_date.isoformat()} across {len(store_keys)} store(s)...")

    total = 0.0
    quantity = 0.0
    failed_stores = []

    def _fetch_one(store_key):
        return ls.fetch_keyword_sales(config, store_key, "mama", snapshot_date, snapshot_date, "pacha")

    with ThreadPoolExecutor(max_workers=max(len(store_keys), 1)) as pool:
        futures = {pool.submit(_fetch_one, store_key): store_key for store_key in store_keys}
        for future in as_completed(futures):
            store_key = futures[future]
            try:
                result = future.result()
            except Exception as e:
                print(f"[mama's sold] [{store_key}] failed: {e}")
                failed_stores.append(store_key)
                continue
            total += result["total"]
            quantity += result["quantity"]

    save_mama_snapshot(snapshot_date, total, quantity, failed_stores)
    print(
        f"[mama's sold] Saved: {quantity:,.0f} units, ${total:,.2f} "
        f"({len(failed_stores)} store(s) failed and were excluded)"
    )


def main():
    config = load_config()
    # Each step runs even if an earlier one fails, so e.g. an ERP outage
    # doesn't also stop the pace log / Mama's Sold from refreshing. Any
    # failure still makes the run exit non-zero (visible in Railway).
    steps = [
        ("pace log", lambda: refresh_pace_log(config)),
        ("mama's sold", lambda: refresh_mama_sold(config)),
        ("top shelf invoices", refresh_topshelf_invoices),
    ]
    failed = []
    for name, step in steps:
        try:
            step()
        except Exception as e:
            print(f"[{name}] FAILED: {e}")
            failed.append(name)
    if failed:
        raise RuntimeError(f"{len(failed)} step(s) failed: {', '.join(failed)}")
    print("Nightly refresh complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Print and exit non-zero so a failed run is visible in Railway's
        # logs/execution list, rather than silently doing nothing.
        print(f"Nightly refresh FAILED: {e}")
        sys.exit(1)

"""
DoorDash sales lookups shared by the Doordash Sales page
(doordash_sales_page.py) and the nightly refresh (nightly_refresh.py ->
morning brief). Plain module - no Streamlit calls - so the nightly cron
job can import it.

Per store:
  1. find_customers: the store's customer record(s) whose first name,
     last name or company matches the search text (default "doordash";
     "doordash" also matches "Door Dash"). Customers are per-account in
     Lightspeed, so every store has its own DoorDash customerID.
  2. fetch_customer_sales: only those customers' completed sales in the
     date range (customerID filter on Sale.json). Voided/archived sales
     are dropped, same as lightspeed_client.fetch_sales().

NOTE: Sale.json only loads SaleLines + Item - NOT "Employee". Some
accounts (the South stores) reject Employee as a relation with a 400.
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dt_time, timedelta, timezone

import lightspeed_client as ls

DEFAULT_SEARCH = "doordash"
CUSTOMER_FIELDS = ("firstName", "lastName", "company")


def search_to_like(search_text):
    """Turns search text into a Lightspeed LIKE pattern. 'doordash' or
    'door dash' -> '%door%dash%' (catches both spellings); any other text
    has its spaces turned into wildcards, e.g. 'dd orders' -> '%dd%orders%'."""
    cleaned = " ".join(search_text.lower().split())
    if cleaned.replace(" ", "") == "doordash":
        return "%door%dash%"
    return "%" + cleaned.replace(" ", "%") + "%"


def num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def as_list(value):
    if not value:
        return []
    return [value] if isinstance(value, dict) else list(value)


def customer_label(customer):
    first = (customer.get("firstName") or "").strip()
    last = (customer.get("lastName") or "").strip()
    company = (customer.get("company") or "").strip()
    name = " ".join(part for part in (first, last) if part)
    if company and company.casefold() != name.casefold():
        name = f"{name} ({company})" if name else company
    return name or f"Customer {customer.get('customerID')}"


def find_customers(config, store_key, like_pattern):
    """Returns {customerID: display name} for every customer at this store
    whose first name, last name or company matches like_pattern."""
    found = {}
    for field in CUSTOMER_FIELDS:
        records = ls.fetch_all(
            config, store_key, "Customer.json",
            params={"limit": 100, field: f"~,{like_pattern}"},
            record_key="Customer",
        )
        for customer in records:
            customer_id = str(customer.get("customerID") or "")
            if customer_id and customer_id != "0":
                found[customer_id] = customer_label(customer)
    return found


def fetch_customer_sales(config, store_key, customer_ids, start_date, end_date):
    """Completed, non-voided sales for the given customerIDs between
    start_date and end_date (inclusive, in the store's local time)."""
    store_tz = ls._get_store_timezone(store_key)
    start_utc = datetime.combine(start_date, dt_time.min, tzinfo=store_tz).astimezone(timezone.utc)
    end_utc = (
        datetime.combine(end_date + timedelta(days=1), dt_time.min, tzinfo=store_tz)
        - timedelta(seconds=1)
    ).astimezone(timezone.utc)

    params = [
        ("limit", 100),
        ("completed", "true"),
        ("timeStamp", f"><,{start_utc.isoformat(timespec='seconds')},{end_utc.isoformat(timespec='seconds')}"),
        ("customerID", f"IN,[{','.join(sorted(customer_ids))}]"),
        ("load_relations", '["SaleLines","SaleLines.Item"]'),
    ]
    sales = ls.fetch_all(config, store_key, "Sale.json", params=params, record_key="Sale")
    return [
        sale for sale in sales
        if str(sale.get("voided", "false")).lower() != "true"
        and str(sale.get("archived", "false")).lower() != "true"
    ]


def sale_total(sale):
    """Sale total including tax."""
    return num(sale.get("calcTotal", sale.get("total")))


def sale_item_count(sale):
    """Units across the sale's lines (a return line counts negative)."""
    return sum(num(line.get("unitQuantity")) for line in as_list((sale.get("SaleLines") or {}).get("SaleLine")))


def fetch_store(config, store_key, start_date, end_date, search_text=DEFAULT_SEARCH):
    """Returns {"customers": {id: name}, "sales": [raw Sale records]} for one store."""
    customers = find_customers(config, store_key, search_to_like(search_text))
    sales = fetch_customer_sales(config, store_key, customers.keys(), start_date, end_date) if customers else []
    return {"customers": customers, "sales": sales}


def fetch_stores(store_keys, start_date, end_date, search_text=DEFAULT_SEARCH):
    """Runs fetch_store for every store in parallel. Returns
    {store_key: {"customers", "sales", "error"}}.

    Stores that fail in the parallel pass are usually ones whose access
    token needed refreshing at the same moment as others - Lightspeed's
    shared OAuth endpoint answers a burst of refreshes with 429 - so those
    are retried one at a time, with a pause, up to twice. Reloading config
    picks up tokens other threads already saved."""
    config = ls.load_config()
    results = {}

    def _one(store_key):
        try:
            return store_key, {**fetch_store(config, store_key, start_date, end_date, search_text), "error": None}
        except Exception as e:
            print(f"[{store_key}] DoorDash sales fetch failed: {e}")
            return store_key, {"customers": {}, "sales": [], "error": str(e)}

    with ThreadPoolExecutor(max_workers=max(len(store_keys), 1)) as pool:
        for future in as_completed([pool.submit(_one, key) for key in store_keys]):
            store_key, result = future.result()
            results[store_key] = result

    for attempt in range(2):
        failed = [key for key in store_keys if results[key]["error"]]
        if not failed:
            break
        time.sleep(3 * (attempt + 1))
        config = ls.load_config()
        for store_key in failed:
            _, results[store_key] = _one(store_key)
            time.sleep(1)
    return results

"""
Lightspeed Retail (R-Series) API client.

Handles:
- Refreshing OAuth access tokens automatically (they expire ~every 30-60 min)
- Pulling sales for a date range
- Pulling employees so we can show names instead of just IDs
- Normalizing the raw API response into simple rows our commission engine can use

NOTE ON FIELD NAMES:
Lightspeed's API can return slightly different field names/shapes depending on
your account's plan/version. The field names below (employeeID, total,
completeTime, SaleLines, calcDiscount) match the standard R-Series V3 API as
documented at developers.lightspeedhq.com. If you get KeyErrors when this
actually runs against your real accounts, run `python inspect_sample.py
store_1` (included in this project) to dump one raw sale to your terminal,
and we'll adjust the field mapping in `normalize_sale()` below together.
"""
import os
import json
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

CONFIG_PATH = "stores_config.json"
TOKEN_URL_TEMPLATE = "https://cloud.lightspeedapp.com/oauth/access_token.php"
API_BASE_TEMPLATE = "https://api.lightspeedapp.com/API/V3/Account/{account_id}"

# Each store's actual local timezone, for correctly converting "yesterday"/
# "this month" etc into UTC boundaries. Default is Central; override any
# store that's genuinely in a different region here.
#
# store_1 and store_6 are set to Eastern despite being confirmed Central-
# time stores (Princeton, TX and Greenville) - this is a known workaround
# for an unexplained ~1hr offset bug discovered while building the
# Reconciliation tool. The root cause was never fully confirmed, but this
# label reliably produces correct results for those two stores.
#
# Everything below that is a REAL Florida store (Eastern time, confirmed
# 2026-08) - not a workaround, just an accurate label. Verify a report
# against real sale times before fully trusting these, same way Princeton/
# Greenville were originally caught.
DEFAULT_TIMEZONE = "America/Chicago"
STORE_TIMEZONE_OVERRIDES = {
    "store_1": "America/New_York",  # Princeton - workaround, see note above
    "store_6": "America/New_York",  # Greenville - workaround, see note above

    # Florida stores (confirmed Eastern, 2026-08)
    "davie": "America/New_York",
    "doral": "America/New_York",
    "delray": "America/New_York",
    "sunset": "America/New_York",
    "boynton": "America/New_York",
    "gateway": "America/New_York",
    "kendall": "America/New_York",
    "killian": "America/New_York",
    "pompano": "America/New_York",
    "sunrise": "America/New_York",
    "aventura": "America/New_York",
    "brickell": "America/New_York",
    "key_west": "America/New_York",
    "lakepark": "America/New_York",
    "las_olas": "America/New_York",
    "palmetto": "America/New_York",
    "surfside": "America/New_York",
    "woc_mimo": "America/New_York",
    "ftl_beach": "America/New_York",
    "hollywood": "America/New_York",
    "homestead": "America/New_York",
    "pinecrest": "America/New_York",
    "commercial": "America/New_York",
    "palm_beach": "America/New_York",
    "sweetwater": "America/New_York",
    "17th_street": "America/New_York",
    "doral_south": "America/New_York",
    "south_beach": "America/New_York",
    "south_miami": "America/New_York",
    "tallahassee": "America/New_York",
    "coral_gables": "America/New_York",
    "downtown_ftl": "America/New_York",
    "wilton_manors": "America/New_York",
    "woc_edgewater": "America/New_York",
    "brickell_center": "America/New_York",
    "flagler_village": "America/New_York",
    "pinecrest_north": "America/New_York",
    "north_miami_beach": "America/New_York",
    "brickell_smoke_shop": "America/New_York",
    "store_12": "America/New_York",  # Lake Worth, FL (not Lake Worth, TX)
}


def _get_store_timezone(store_key):
    tz_name = STORE_TIMEZONE_OVERRIDES.get(store_key, DEFAULT_TIMEZONE)
    return ZoneInfo(tz_name)


def _get_db_connection():
    """Returns a psycopg2 connection if DATABASE_URL is set (e.g. Railway's
    Postgres add-on), or None if no database is configured. Kept as a
    lazy import so environments without psycopg2 installed don't break.

    Uses a short connect_timeout so a database problem (unreachable host,
    networking issue) shows up as a fast, visible error instead of hanging
    the whole app indefinitely - an indefinite hang here is what Railway
    reports as a generic 'Application failed to respond' with no useful
    detail in the logs."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return None
    import psycopg2
    return psycopg2.connect(database_url, connect_timeout=10)


def _ensure_config_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS app_config (
                id INTEGER PRIMARY KEY,
                config_json JSONB NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
    conn.commit()


def load_config():
    """
    Loads the full config dict (client_id/secret + all stores). Tries, in
    order:
      1. A Postgres database (if DATABASE_URL is set) - this is what makes
         changes (like adding a new store) actually persist on Railway,
         where the local filesystem doesn't survive restarts/redeploys.
      2. The STORES_CONFIG_JSON environment variable (legacy Railway setup).
      3. The local stores_config.json file (local development).
    The returned dict shape is identical regardless of source, so every
    other file that calls load_config() doesn't need to know or care where
    the data actually came from.
    """
    conn = _get_db_connection()
    if conn:
        try:
            _ensure_config_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT config_json FROM app_config WHERE id = 1")
                row = cur.fetchone()
                if row:
                    return row[0]
        finally:
            conn.close()
        # Database is reachable but has no config saved yet - fall through
        # to bootstrap from the env var/file below. The next save_config()
        # call will persist it into the database from then on.

    config_json = os.getenv("STORES_CONFIG_JSON")
    if config_json:
        return json.loads(config_json)

    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(config):
    """
    Saves the full config dict. If a database is configured, this actually
    persists (survives restarts/redeploys) - this is required for the
    in-app "Add Store" flow to work reliably. Falls back to the old
    file-based behavior for local development without a database.
    """
    conn = _get_db_connection()
    if conn:
        try:
            _ensure_config_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_config (id, config_json, updated_at)
                    VALUES (1, %s, now())
                    ON CONFLICT (id) DO UPDATE
                        SET config_json = EXCLUDED.config_json,
                            updated_at = now()
                    """,
                    (json.dumps(config),),
                )
            conn.commit()
            return
        finally:
            conn.close()

    # No database configured - old behavior for local dev.
    if os.getenv("STORES_CONFIG_JSON"):
        return
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def _token_is_expired(store_cfg):
    expires_at = store_cfg.get("token_expires_at")
    if not expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    # refresh a bit early to be safe
    return datetime.now(timezone.utc) >= expiry.replace(tzinfo=timezone.utc)


def refresh_access_token(config, store_key):
    """Uses the stored refresh_token to get a new access_token for one store."""
    store_cfg = config["stores"][store_key]
    resp = requests.post(
        TOKEN_URL_TEMPLATE,
        data={
            "refresh_token": store_cfg["refresh_token"],
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    store_cfg["access_token"] = payload["access_token"]
    expires_in = int(payload.get("expires_in", 1800))
    store_cfg["token_expires_at"] = (
        datetime.now(timezone.utc).isoformat()
    )
    # store expiry as now + expires_in seconds, minus a small buffer
    expiry_ts = time.time() + expires_in - 60
    store_cfg["token_expires_at"] = datetime.fromtimestamp(
        expiry_ts, tz=timezone.utc
    ).isoformat()

    save_config(config)
    return store_cfg["access_token"]


def get_valid_token(config, store_key):
    store_cfg = config["stores"][store_key]
    if not store_cfg.get("refresh_token"):
        raise RuntimeError(
            f"{store_key} has not been connected yet. Run oauth_setup.py first."
        )
    if _token_is_expired(store_cfg):
        return refresh_access_token(config, store_key)
    return store_cfg["access_token"]


def api_get(config, store_key, path, params=None):
    """Makes an authenticated GET request against one store's API, auto-retrying
    once on a 401 in case the token just expired mid-session."""
    store_cfg = config["stores"][store_key]
    token = get_valid_token(config, store_key)
    account_id = store_cfg["account_id"]
    url = f"{API_BASE_TEMPLATE.format(account_id=account_id)}/{path}"
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code == 401:
        token = refresh_access_token(config, store_key)
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code >= 400:
        raise RuntimeError(
            f"Lightspeed API error {resp.status_code} on {path}: {resp.text}"
        )
    return resp.json()


def fetch_employees(config, store_key):
    """Returns {employeeID: display_name}"""
    data = api_get(config, store_key, "Employee.json", params={"limit": 100})
    employees = {}
    raw = data.get("Employee", [])
    if isinstance(raw, dict):  # single employee comes back as dict, not list
        raw = [raw]
    for emp in raw:
        emp_id = emp.get("employeeID")
        first = emp.get("firstName", "")
        last = emp.get("lastName", "")
        employees[emp_id] = f"{first} {last}".strip() or f"Employee {emp_id}"
    return employees



def fetch_discounts(config, store_key):
    """Returns {discountID: discount_name} for one store.

    Lightspeed sale lines contain only discountID, so promo commissions must
    resolve that ID against the Discount endpoint. Results are fetched once
    per report run and reused for every sale.
    """
    discounts = {}
    data = api_get(config, store_key, "Discount.json", params={"limit": 100})
    seen_urls = set()

    while True:
        raw = data.get("Discount", [])
        if isinstance(raw, dict):
            raw = [raw]
        for discount in raw:
            discount_id = str(discount.get("discountID", ""))
            name = str(discount.get("name", "") or "").strip()
            if discount_id:
                discounts[discount_id] = name

        next_url = (data.get("@attributes", {}) or {}).get("next")
        if not next_url or next_url in seen_urls:
            break
        seen_urls.add(next_url)
        data = api_get_full_url(config, store_key, next_url)

    return discounts


def fetch_categories(config, store_key):
    """Returns {categoryID: category_name} for one store.

    Categories are per-account in Lightspeed, so the same category name
    (e.g. "Rolling Trays") can have a different categoryID at each store.
    Callers that need a cross-store dropdown should merge by name and look
    up each store's own categoryID separately (see category_sales page).
    """
    categories = {}
    data = api_get(config, store_key, "Category.json", params={"limit": 100})
    seen_urls = set()

    while True:
        raw = data.get("Category", [])
        if isinstance(raw, dict):
            raw = [raw]
        for category in raw:
            category_id = str(category.get("categoryID", ""))
            name = str(category.get("name", "") or "").strip()
            if category_id and name:
                categories[category_id] = name

        next_url = (data.get("@attributes", {}) or {}).get("next")
        if not next_url or next_url in seen_urls:
            break
        seen_urls.add(next_url)
        data = api_get_full_url(config, store_key, next_url)

    return categories


def fetch_items_by_category(config, store_key, category_id):
    """Returns a set of itemIDs belonging to one category at one store.

    Filters server-side via Lightspeed's categoryID query param so we don't
    have to page through the entire item catalog for every lookup.
    """
    item_ids = set()
    data = api_get(
        config,
        store_key,
        "Item.json",
        params={"limit": 100, "categoryID": category_id},
    )
    seen_urls = set()

    while True:
        raw = data.get("Item", [])
        if isinstance(raw, dict):
            raw = [raw]
        for item in raw:
            item_id = str(item.get("itemID", ""))
            if item_id:
                item_ids.add(item_id)

        next_url = (data.get("@attributes", {}) or {}).get("next")
        if not next_url or next_url in seen_urls:
            break
        seen_urls.add(next_url)
        data = api_get_full_url(config, store_key, next_url)

    return item_ids


def fetch_items_by_keyword(config, store_key, keyword):
    """Returns a set of itemIDs whose description contains the given keyword
    (case-insensitive substring match) at one store.

    Uses Lightspeed's "~" LIKE operator with % wildcards on both sides, so
    "mug" matches "Ceramic Mug", "Travel Mug 16oz", "Mugshot Ale Glass", etc.
    """
    item_ids = set()
    data = api_get(
        config,
        store_key,
        "Item.json",
        params={"limit": 100, "description": f"~,%{keyword}%"},
    )
    seen_urls = set()

    while True:
        raw = data.get("Item", [])
        if isinstance(raw, dict):
            raw = [raw]
        for item in raw:
            item_id = str(item.get("itemID", ""))
            if item_id:
                item_ids.add(item_id)

        next_url = (data.get("@attributes", {}) or {}).get("next")
        if not next_url or next_url in seen_urls:
            break
        seen_urls.add(next_url)
        data = api_get_full_url(config, store_key, next_url)

    return item_ids


def fetch_items_by_upc(config, store_key, upc):
    """Returns a set of itemIDs whose UPC exactly matches at one store.

    Unlike keyword search, UPC is an exact-match lookup rather than a
    substring match - a UPC either matches one item or it doesn't.
    """
    item_ids = set()
    data = api_get(
        config,
        store_key,
        "Item.json",
        params={"limit": 100, "upc": upc},
    )
    seen_urls = set()

    while True:
        raw = data.get("Item", [])
        if isinstance(raw, dict):
            raw = [raw]
        for item in raw:
            item_id = str(item.get("itemID", ""))
            if item_id:
                item_ids.add(item_id)

        next_url = (data.get("@attributes", {}) or {}).get("next")
        if not next_url or next_url in seen_urls:
            break
        seen_urls.add(next_url)
        data = api_get_full_url(config, store_key, next_url)

    return item_ids


ITEM_BATCH_SIZE = 50  # keeps the itemID filter well under typical URL length limits
SALE_ID_BATCH_SIZE = 50  # same, for the follow-up Sale status lookup


def fetch_item_ids_sales(config, store_key, item_ids, start_date, end_date):
    """
    Sums sales for one store, for a given set of itemIDs, over a date range.

    Shared by fetch_category_sales and fetch_keyword_sales - the only
    difference between "category" and "keyword" search is how the itemID
    set is resolved beforehand; once we have it, the sales lookup is
    identical.

    Queries SaleLine.json directly (instead of pulling every full Sale and
    filtering in Python) so Lightspeed does the filtering server-side:
      1. itemID filtered to just this set, via the "IN" operator, batched
         at ITEM_BATCH_SIZE items per request, and timeStamp filtered to
         the date range - SaleLine carries its own timeStamp/createTime/
         completeTime fields directly, so no relation load is needed for
         this step (SaleLine.json rejects load_relations=["Sale"] with a
         400 - the relation isn't loadable from this endpoint).
      2. A small follow-up Sale.json lookup, batched by the saleIDs actually
         referenced in step 1 (not every sale in the date range), to check
         completed/voided/archived - fields that only live on Sale.

    NOTE ON THE "IN" OPERATOR: this follows the documented Lightspeed
    R-Series query syntax (field=operator,value1,value2,...), the same
    style your existing timeStamp "between" filter uses. If either step
    errors, dump one raw SaleLine/Sale with `inspect_sample.py` and we'll
    adjust the param format together - same as the note at the top of this
    file.

    Returns {"total": float, "quantity": float}
    """
    item_ids = sorted(item_ids)
    if not item_ids:
        return {"total": 0.0, "quantity": 0.0}

    store_timezone = _get_store_timezone(store_key)
    start_local = datetime.combine(start_date, dt_time.min, tzinfo=store_timezone)
    end_local_exclusive = datetime.combine(
        end_date + timedelta(days=1), dt_time.min, tzinfo=store_timezone
    )
    start_utc = start_local.astimezone(timezone.utc)
    end_utc_inclusive = end_local_exclusive.astimezone(timezone.utc) - timedelta(seconds=1)

    def _ts(value):
        return value.isoformat(timespec="seconds")

    # Step 1: pull matching sale lines (itemID set + date range), server-side filtered.
    matched_lines = []
    sale_ids = set()

    for i in range(0, len(item_ids), ITEM_BATCH_SIZE):
        batch = item_ids[i:i + ITEM_BATCH_SIZE]
        params = [
            ("limit", 100),
            ("itemID", "IN," + ",".join(batch)),
            (
                "timeStamp",
                f"><,{_ts(start_utc)},{_ts(end_utc_inclusive)}",
            ),
        ]
        data = api_get(config, store_key, "SaleLine.json", params=params)
        seen_urls = set()

        while True:
            raw = data.get("SaleLine", [])
            if isinstance(raw, dict):
                raw = [raw]
            for line in raw:
                matched_lines.append(line)
                sale_id = str(line.get("saleID", "") or "")
                if sale_id:
                    sale_ids.add(sale_id)

            next_url = (data.get("@attributes", {}) or {}).get("next")
            if not next_url or next_url in seen_urls:
                break
            seen_urls.add(next_url)
            data = api_get_full_url(config, store_key, next_url)

    if not matched_lines:
        return {"total": 0.0, "quantity": 0.0}

    # Step 2: check completed/voided/archived only for the sales actually touched.
    valid_sale_ids = set()
    sale_ids_list = sorted(sale_ids)
    for i in range(0, len(sale_ids_list), SALE_ID_BATCH_SIZE):
        batch = sale_ids_list[i:i + SALE_ID_BATCH_SIZE]
        params = [
            ("limit", 100),
            ("saleID", "IN," + ",".join(batch)),
        ]
        data = api_get(config, store_key, "Sale.json", params=params)
        seen_urls = set()

        while True:
            raw = data.get("Sale", [])
            if isinstance(raw, dict):
                raw = [raw]
            for sale in raw:
                if str(sale.get("completed", "true")).lower() != "true":
                    continue
                if str(sale.get("voided", "false")).lower() == "true":
                    continue
                if str(sale.get("archived", "false")).lower() == "true":
                    continue
                valid_sale_ids.add(str(sale.get("saleID", "")))

            next_url = (data.get("@attributes", {}) or {}).get("next")
            if not next_url or next_url in seen_urls:
                break
            seen_urls.add(next_url)
            data = api_get_full_url(config, store_key, next_url)

    total = 0.0
    quantity = 0.0
    for line in matched_lines:
        if str(line.get("saleID", "") or "") not in valid_sale_ids:
            continue
        line_total = float(
            line.get("calcTotal", line.get("displayableSubtotal", 0)) or 0
        )
        total += line_total
        quantity += abs(float(line.get("unitQuantity", 1) or 1))

    return {"total": total, "quantity": quantity}


def fetch_category_sales(config, store_key, category_id, start_date, end_date):
    """Sums sales for one store/category over a date range.
    See fetch_item_ids_sales for how the sales lookup itself works.
    """
    item_ids = fetch_items_by_category(config, store_key, category_id)
    return fetch_item_ids_sales(config, store_key, item_ids, start_date, end_date)


def fetch_keyword_sales(config, store_key, keyword, start_date, end_date):
    """Sums sales for one store, for all items whose description contains
    the given keyword, over a date range.
    See fetch_item_ids_sales for how the sales lookup itself works.
    """
    item_ids = fetch_items_by_keyword(config, store_key, keyword)
    return fetch_item_ids_sales(config, store_key, item_ids, start_date, end_date)


def fetch_upc_sales(config, store_key, upc, start_date, end_date):
    """Sums sales for one store, for the item matching the given UPC,
    over a date range.
    See fetch_item_ids_sales for how the sales lookup itself works.
    """
    item_ids = fetch_items_by_upc(config, store_key, upc)
    return fetch_item_ids_sales(config, store_key, item_ids, start_date, end_date)


def api_get_full_url(config, store_key, url):
    """Same as api_get but for following a full pagination URL Lightspeed
    gives us directly (used for cursor-based pagination)."""
    token = get_valid_token(config, store_key)
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 401:
        token = refresh_access_token(config, store_key)
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Lightspeed API error {resp.status_code}: {resp.text}")
    return resp.json()


def fetch_sales(config, store_key, start_date, end_date):
    """
    Pulls all completed sales for a store between start_date and end_date
    (inclusive), following Lightspeed's cursor-based pagination (the old
    offset-based method is deprecated).

    start_date / end_date: datetime.date objects
    """
    all_sales = []
    limit = 100
    max_pages = 50  # safety net: 50 pages * 100 = 5,000 sales, plenty for one store/period; stops runaway loops fast

    # Lightspeed stores timestamps in UTC, while the website report uses the
    # store's local business dates. Convert the selected dates into UTC
    # boundaries using THIS STORE'S actual timezone (see
    # STORE_TIMEZONE_OVERRIDES above) so the date range means midnight in
    # that store's own region, not always Texas.
    store_timezone = _get_store_timezone(store_key)
    start_local = datetime.combine(start_date, dt_time.min, tzinfo=store_timezone)
    end_local_exclusive = datetime.combine(
        end_date + timedelta(days=1), dt_time.min, tzinfo=store_timezone
    )
    start_utc = start_local.astimezone(timezone.utc)
    end_utc_exclusive = end_local_exclusive.astimezone(timezone.utc)

    # Use an exclusive upper boundary at the following local midnight. Subtract
    # one second because Lightspeed's between filter is inclusive.
    end_utc_inclusive = end_utc_exclusive - timedelta(seconds=1)

    def _lightspeed_timestamp(value):
        return value.isoformat(timespec="seconds")

    # Lightspeed's "between" range filter: operator + comma-separated bounds.
    params = [
        ("limit", limit),
        ("completed", "true"),
        (
            "timeStamp",
            f"><,{_lightspeed_timestamp(start_utc)},"
            f"{_lightspeed_timestamp(end_utc_inclusive)}",
        ),
        ("load_relations", '["SaleLines"]'),
    ]
    print(f"[{store_key}] Requesting page 1 of sales...")
    data = api_get(config, store_key, "Sale.json", params=params)

    page = 1
    seen_urls = set()
    while True:
        raw = data.get("Sale", [])
        if isinstance(raw, dict):
            raw = [raw]
        # The API can still return completed records that were later voided or
        # archived. Those should not count as employee sales or commissions.
        valid_sales = [
            sale for sale in raw
            if str(sale.get("voided", "false")).lower() != "true"
            and str(sale.get("archived", "false")).lower() != "true"
        ]
        all_sales.extend(valid_sales)
        skipped = len(raw) - len(valid_sales)
        print(
            f"[{store_key}] Page {page}: got {len(raw)} sales, "
            f"kept {len(valid_sales)} and skipped {skipped} voided/archived "
            f"(running total: {len(all_sales)})"
        )

        next_url = (data.get("@attributes", {}) or {}).get("next")
        if not next_url or next_url in seen_urls:
            break
        if page >= max_pages:
            print(f"[{store_key}] Hit the {max_pages}-page safety limit, stopping early.")
            break

        seen_urls.add(next_url)
        page += 1
        print(f"[{store_key}] Requesting page {page}...")
        data = api_get_full_url(config, store_key, next_url)

    print(f"[{store_key}] Done. Total sales fetched: {len(all_sales)}")

    return all_sales


def normalize_sale(
    sale, employees, store_key, promo_names_to_track, discount_names_by_id
):
    """
    Convert one raw Sale record into the flat structure used by the
    commission engine.

    ``promo_matches`` counts matching discounted sale lines.
    ``promo_transactions`` records whether each promo appeared at least once
    on the transaction. Individual commission rules can therefore choose
    whether a conversion means a discounted item/line or one transaction.
    """
    sale_employee_id = str(sale.get("employeeID", "") or "")
    total = float(sale.get("total", sale.get("calcTotal", 0)) or 0)
    subtotal = float(
        sale.get("calcSubtotal", sale.get("displayableSubtotal", 0)) or 0
    )
    discount = abs(float(sale.get("calcDiscount", sale.get("discount", 0)) or 0))
    net_subtotal = max(subtotal - discount, 0.0)

    lines = sale.get("SaleLines", {}).get("SaleLine", [])
    if isinstance(lines, dict):
        lines = [lines]

    # Lightspeed can store the cashier on the Sale itself while the employee
    # who actually owns the sale is stored on each SaleLine. This caused some
    # transactions (especially between Elijah and Angel) to be assigned to the
    # wrong person. Prefer the line-level employee when all sale lines agree.
    # For genuinely mixed-employee tickets, keep the sale-level employee when
    # it is one of the line employees; otherwise use the employee attached to
    # the greatest item quantity.
    line_employee_quantities = {}
    for line in lines:
        line_employee_id = str(line.get("employeeID", "") or "")
        if not line_employee_id or line_employee_id == "0":
            continue
        quantity = abs(float(line.get("unitQuantity", 1) or 1))
        line_employee_quantities[line_employee_id] = (
            line_employee_quantities.get(line_employee_id, 0) + quantity
        )

    if len(line_employee_quantities) == 1:
        employee_id = next(iter(line_employee_quantities))
    elif sale_employee_id in line_employee_quantities:
        employee_id = sale_employee_id
    elif line_employee_quantities:
        employee_id = max(line_employee_quantities, key=line_employee_quantities.get)
    else:
        employee_id = sale_employee_id

    promo_matches = {name: 0 for name in promo_names_to_track}
    promo_transactions = {name: 0 for name in promo_names_to_track}

    normalized_config_names = {
        name: name.strip().casefold()
        for name in promo_names_to_track
        if name
    }

    for line in lines:
        discount_id = str(line.get("discountID", "0") or "0")
        discount_name = discount_names_by_id.get(discount_id, "").strip()
        if not discount_name:
            continue

        normalized_discount_name = discount_name.casefold()
        for configured_name, normalized_config_name in normalized_config_names.items():
            if normalized_config_name == normalized_discount_name:
                quantity = abs(float(line.get("unitQuantity", 1) or 1))
                promo_matches[configured_name] += quantity
                promo_transactions[configured_name] = 1

    return {
        "store": store_key,
        "employee_id": employee_id,
        "employee_name": employees.get(employee_id, f"Employee {employee_id}"),
        "total": total,
        "subtotal": subtotal,
        "discount": discount,
        "net_subtotal": net_subtotal,
        "promo_matches": promo_matches,
        "promo_transactions": promo_transactions,
    }


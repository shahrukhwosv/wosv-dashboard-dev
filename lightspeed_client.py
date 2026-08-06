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


def load_config():
    config_json = os.getenv("STORES_CONFIG_JSON")

    if config_json:
        return json.loads(config_json)

    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(config):
    # On Railway, configuration comes from an environment variable.
    # Writing stores_config.json during a token refresh causes Streamlit to
    # detect a file change and rerun the app before the report is displayed.
    # Keep refreshed tokens in memory for the current report instead.
    if os.getenv("STORES_CONFIG_JSON"):
        return

    # Local setup still saves OAuth credentials to stores_config.json.
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
    # store's local business dates. Convert the selected Central Time dates
    # into UTC boundaries so June 7 means midnight in Texas—not midnight UTC.
    store_timezone = ZoneInfo("America/Chicago")
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


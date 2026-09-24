"""
Client for the Top Shelf Novelties Salesgent ERP (erp.topshelfnovelties.com).

Salesgent doesn't offer a public API, so this uses the same JSON API the
ERP's own web app calls (visible in the browser's Network tab). It is not
officially supported by Salesgent - endpoint names or response shapes can
change without notice. If this breaks, open the ERP in Chrome, press F12,
go to the Network tab, reload the page in question, and compare the /api/
calls against the ones used below.

AUTH (how the ERP keeps a login alive):
  - Logging in needs email + password AND a one-time code (OTP) emailed to
    the account, so a script can't log in fully on its own.
  - A login produces an access token (~33 hours) and a refresh token
    (~3 days). POST /api/refreshToken swaps them for a brand new pair, so
    as long as something refreshes at least once every ~3 days, the
    connection stays alive indefinitely without another OTP.
  - Tokens are stored in the Postgres database (table erp_auth) so the
    nightly cron job and the web app share them. The "Connect ERP" form on
    the Top Shelf Invoices page (admin only) does the one-time login + OTP.
  - If the nightly job fails 3+ nights in a row the refresh token expires
    and an admin has to reconnect from that page.

Use a dedicated, read-only ERP user for this - not a personal admin login.
"""
import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from lightspeed_client import _get_db_connection

ERP_BASE_URL = os.getenv("ERP_BASE_URL", "https://erp.topshelfnovelties.com").rstrip("/")
ERP_STORE_IDS = os.getenv("ERP_STORE_IDS", "1,2")  # the ERP's own store ids (Cash & Carry + Ecommerce)
ERP_TIMEZONE = ZoneInfo("America/Chicago")

# Refresh the access token when it has less than this long left. The
# nightly job runs every ~24h and access tokens last ~33h, so this makes
# each nightly run roll the token pair forward (keeping the 3-day refresh
# token from ever running out) while the web app, which also reads these
# tokens, rarely needs to refresh on its own.
REFRESH_WHEN_LESS_THAN = timedelta(hours=12)

# Device info sent with the OTP calls - the ERP records it as the "device"
# that was authorized, so it shows up recognizably in the ERP's device list.
_DEVICE_INFO = {
    "deviceName": "WOSV Dashboard",
    "deviceOperatingSystem": "Linux server",
    "deviceBrowser": "python-requests",
}


class ErpAuthError(RuntimeError):
    """Raised when there's no usable ERP login - an admin needs to
    (re)connect from the Top Shelf Invoices page."""


# ---------------------------------------------------------------------------
# Token storage (Postgres)
# ---------------------------------------------------------------------------

def _require_db():
    conn = _get_db_connection()
    if conn is None:
        raise RuntimeError("DATABASE_URL is not set - the ERP connection needs the database.")
    return conn


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS erp_auth (
                id INTEGER PRIMARY KEY,
                username TEXT,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()


def _load_tokens():
    conn = _require_db()
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT username, access_token, refresh_token, updated_at FROM erp_auth WHERE id = 1")
            row = cur.fetchone()
        if not row:
            return None
        return {"username": row[0], "access": row[1], "refresh": row[2], "updated_at": row[3]}
    finally:
        conn.close()


def _save_tokens(access, refresh, username=None):
    conn = _require_db()
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO erp_auth (id, username, access_token, refresh_token, updated_at)
                VALUES (1, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    username = COALESCE(EXCLUDED.username, erp_auth.username),
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    updated_at = now()
                """,
                (username, access, refresh),
            )
        conn.commit()
    finally:
        conn.close()


def _token_expiry(token):
    """Reads the `exp` claim out of a JWT without verifying it (we only
    need to know when to refresh - the ERP does the real verification)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        return None


def connection_status():
    """For display on the page: who's connected and when the login
    would lapse if nothing refreshed it."""
    tokens = _load_tokens()
    if not tokens:
        return None
    return {
        "username": tokens["username"],
        "updated_at": tokens["updated_at"],
        "refresh_expires": _token_expiry(tokens["refresh"]),
    }


# ---------------------------------------------------------------------------
# One-time connect (login + OTP) - driven from the page's admin form
# ---------------------------------------------------------------------------

def _unwrap(resp, what):
    try:
        data = resp.json()
    except ValueError:
        raise ErpAuthError(f"{what} failed (HTTP {resp.status_code}, non-JSON response).")
    if resp.status_code >= 400 or data.get("hasError"):
        msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
        raise ErpAuthError(f"{what} failed (HTTP {resp.status_code}): {msg or data}")
    return data.get("result")


def start_login(username, password):
    """Step 1: email + password, then ask the ERP to email a one-time
    code. Returns the pending tokens, which only become usable once
    finish_login() validates the code."""
    resp = requests.post(
        f"{ERP_BASE_URL}/api/authenticate",
        json={"username": username, "password": password, "userType": "EMPLOYEE"},
        timeout=30,
    )
    result = _unwrap(resp, "Login")
    access, refresh = (result or {}).get("access"), (result or {}).get("refresh")
    if not access or not refresh:
        raise ErpAuthError("Login response didn't include tokens - the ERP's login API may have changed.")

    now = datetime.now(ERP_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    otp_resp = requests.post(
        f"{ERP_BASE_URL}/api/userLoginDevice/sendOTP",
        json={**_DEVICE_INFO, "insertedTimestamp": now},
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    _unwrap(otp_resp, "Sending the one-time code")
    return {"username": username, "access": access, "refresh": refresh}


def finish_login(pending, otp_code):
    """Step 2: validate the emailed code, then confirm the tokens work and
    save them for the nightly job."""
    now = datetime.now(ERP_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    resp = requests.post(
        f"{ERP_BASE_URL}/api/userLoginDevice/validateOTP",
        json={**_DEVICE_INFO, "otpCode": str(otp_code).strip(), "insertedTimestamp": now},
        headers={"Authorization": f"Bearer {pending['access']}"},
        timeout=30,
    )
    _unwrap(resp, "Validating the one-time code")

    check = requests.get(
        f"{ERP_BASE_URL}/api/order/list",
        params={"storeIds": ERP_STORE_IDS, "page": 0, "size": 1},
        headers={"Authorization": f"Bearer {pending['access']}"},
        timeout=30,
    )
    if check.status_code != 200:
        raise ErpAuthError(
            f"Code accepted, but the ERP still refused an invoice lookup (HTTP {check.status_code}). "
            "Check that this ERP user has permission to view invoices."
        )
    _save_tokens(pending["access"], pending["refresh"], username=pending["username"])


# ---------------------------------------------------------------------------
# Authenticated API calls
# ---------------------------------------------------------------------------

def _refresh(tokens):
    resp = requests.post(
        f"{ERP_BASE_URL}/api/refreshToken",
        headers={
            "refreshToken": tokens["refresh"],
            "Authorization": f"Bearer {tokens['access']}",
            "Accept": "application/json, text/plain",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise ErpAuthError(
            f"ERP token refresh failed (HTTP {resp.status_code}) - the saved login has probably "
            "expired. An admin needs to reconnect from the Top Shelf Invoices page."
        )
    result = resp.json().get("result") or {}
    if not result.get("access"):
        raise ErpAuthError("ERP token refresh returned no access token.")
    new = {**tokens, "access": result["access"], "refresh": result.get("refresh") or tokens["refresh"]}
    _save_tokens(new["access"], new["refresh"])
    return new


class ErpClient:
    def __init__(self):
        tokens = _load_tokens()
        if not tokens:
            raise ErpAuthError("The ERP isn't connected yet. An admin needs to connect it from the Top Shelf Invoices page.")
        exp = _token_expiry(tokens["access"])
        if exp is None or exp - datetime.now(timezone.utc) < REFRESH_WHEN_LESS_THAN:
            tokens = _refresh(tokens)
        self._tokens = tokens
        self._session = requests.Session()

    def get(self, path, params=None, _retried=False):
        resp = self._session.get(
            f"{ERP_BASE_URL}/api{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._tokens['access']}"},
            timeout=60,
        )
        if resp.status_code == 401 and not _retried:
            self._tokens = _refresh(self._tokens)
            return self.get(path, params, _retried=True)
        if resp.status_code == 429 and not _retried:
            time.sleep(5)
            return self.get(path, params, _retried=True)
        resp.raise_for_status()
        return resp.json().get("result")

    # -- invoices -----------------------------------------------------------

    def list_invoices(self, day):
        """Every invoice created on `day` (a date, in Central time)."""
        start_local = datetime.combine(day, datetime.min.time(), tzinfo=ERP_TIMEZONE)
        end_local = start_local + timedelta(days=1) - timedelta(seconds=1)
        fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        invoices, page = [], 0
        while True:
            result = self.get("/order/list", {
                "storeIds": ERP_STORE_IDS,
                "startDate": fmt(start_local),
                "endDate": fmt(end_local),
                "page": page,
                "size": 100,
            })
            invoices.extend(result.get("content") or [])
            if result.get("last", True):
                return invoices
            page += 1

    def invoice_header(self, invoice_id):
        """Invoice-level totals (totalAmount, taxAmount, shippingAmount, ...)."""
        result = self.get(f"/order/{invoice_id}/withCustomer", {"storeIds": ERP_STORE_IDS})
        return (result or {}).get("orderDto") or {}

    def invoice_line_items(self, invoice_id):
        result = self.get(f"/order/lineItem/{invoice_id}", {"storeIds": ERP_STORE_IDS, "sortedByProductName": "true"})
        return [li for li in (result or []) if not li.get("deleted")]

    # -- products -----------------------------------------------------------

    def products_by_upc(self, upc):
        """Products whose UPC is exactly `upc`. Same call the ERP's Product
        List search box makes - that search also matches name/SKU, so the
        results are filtered down to exact UPC matches here. Several color
        variants can share one UPC; each result has costPrice."""
        upc = str(upc).strip()
        if not upc:
            return []
        result = self.get("/product/list", {
            "storeIds": ERP_STORE_IDS, "name": upc, "sku": upc, "upc": upc, "page": 0, "size": 50,
        })
        return [p for p in (result or {}).get("content") or [] if str(p.get("upc") or "").strip() == upc]

    def invoice_shipments(self, invoice_id):
        """Shipment records the ERP keeps for this invoice - for ShipStation
        orders, channelOrderId is the ShipStation orderId."""
        result = self.get(f"/order/shipment/{invoice_id}")
        if not result:
            return []
        return result if isinstance(result, list) else [result]

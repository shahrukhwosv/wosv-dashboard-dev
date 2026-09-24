"""
Client for the Etsy Open API v3 - read-only access to the shop's orders
(receipts), payments and payment-account ledger (fees).

SETUP (one time):
  1. On etsy.com/developers, logged in as the shop owner, create an app
     ("Your apps" > "Create a new app"). It starts as "Pending personal
     approval" - personal-access apps can talk to the owner's own shop.
  2. Add a callback URL on the app that points at this page on the live
     dashboard, EXACTLY (no trailing slash), e.g.
         https://<your-railway-domain>/etsy_orders
  3. Set these environment variables in Railway (web app AND the nightly
     cron service):
         ETSY_API_KEY        the app's "Keystring"
         ETSY_SHARED_SECRET  the app's "Shared secret"
         ETSY_REDIRECT_URI   the exact callback URL from step 2
  4. As an admin, open Etsy Orders > "Admin: Etsy connection" > Connect
     Etsy, approve on Etsy, and you land back on the page connected.

AUTH (how the connection stays alive):
  - OAuth 2.0 authorization code + PKCE. Access tokens last 1 hour;
    refresh tokens last 90 days, and every refresh returns a new refresh
    token - so the nightly job keeps the connection alive indefinitely.
    If nothing runs for 90 days, an admin has to reconnect.
  - Tokens live in Postgres (table etsy_auth), shared by the web app and
    the nightly job, same pattern as topshelf_erp.py.
  - Every request also sends x-api-key: "<keystring>:<shared secret>"
    (Etsy requires the shared secret in that header as of 2026).

SCOPES requested: transactions_r (receipts, payments, ledger), shops_r,
listings_r. Nothing is ever written to Etsy.

RATE LIMITS: ~10 requests/second, 10,000/day. 429s are retried after the
Retry-After delay.
"""
import base64
import hashlib
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse, parse_qs

import requests

from lightspeed_client import _get_db_connection

API_BASE = "https://api.etsy.com/v3"
AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
SCOPES = "transactions_r shops_r listings_r"

# Refresh the access token when it has less than this long left.
REFRESH_WHEN_LESS_THAN = timedelta(minutes=5)
# A PKCE login attempt that isn't finished within this long is discarded.
PENDING_LOGIN_TTL = timedelta(minutes=30)


class EtsyAuthError(RuntimeError):
    """No usable Etsy connection - an admin needs to (re)connect from the
    Etsy Orders page."""


def is_configured():
    return bool(os.getenv("ETSY_API_KEY") and os.getenv("ETSY_SHARED_SECRET") and os.getenv("ETSY_REDIRECT_URI"))


def _api_key_header():
    return f"{os.getenv('ETSY_API_KEY')}:{os.getenv('ETSY_SHARED_SECRET')}"


def _client_id():
    return os.getenv("ETSY_API_KEY")


# ---------------------------------------------------------------------------
# Token storage (Postgres)
# ---------------------------------------------------------------------------

def _require_db():
    conn = _get_db_connection()
    if conn is None:
        raise RuntimeError("DATABASE_URL is not set - the Etsy connection needs the database.")
    return conn


def _ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS etsy_auth (
                id INTEGER PRIMARY KEY,
                etsy_user_id BIGINT,
                shop_id BIGINT,
                shop_name TEXT,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                access_expires_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS etsy_oauth_pending (
                state TEXT PRIMARY KEY,
                code_verifier TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()


def _load_tokens():
    conn = _require_db()
    try:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT etsy_user_id, shop_id, shop_name, access_token, refresh_token, access_expires_at, updated_at "
                "FROM etsy_auth WHERE id = 1"
            )
            row = cur.fetchone()
        if not row:
            return None
        keys = ["user_id", "shop_id", "shop_name", "access", "refresh", "expires_at", "updated_at"]
        return dict(zip(keys, row))
    finally:
        conn.close()


def _save_tokens(access, refresh, expires_in, user_id=None, shop_id=None, shop_name=None):
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in or 3600))
    conn = _require_db()
    try:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO etsy_auth (id, etsy_user_id, shop_id, shop_name, access_token, refresh_token, access_expires_at, updated_at)
                VALUES (1, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    etsy_user_id = COALESCE(EXCLUDED.etsy_user_id, etsy_auth.etsy_user_id),
                    shop_id = COALESCE(EXCLUDED.shop_id, etsy_auth.shop_id),
                    shop_name = COALESCE(EXCLUDED.shop_name, etsy_auth.shop_name),
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    access_expires_at = EXCLUDED.access_expires_at,
                    updated_at = now()
                """,
                (user_id, shop_id, shop_name, access, refresh, expires_at),
            )
        conn.commit()
    finally:
        conn.close()


def connection_status():
    """For display on the page: which shop is connected and when the
    connection would lapse if nothing refreshed it."""
    tokens = _load_tokens()
    if not tokens:
        return None
    return {
        "shop_name": tokens["shop_name"],
        "shop_id": tokens["shop_id"],
        "updated_at": tokens["updated_at"],
        # The refresh token is re-issued on every refresh and lasts 90 days.
        "refresh_expires": tokens["updated_at"] + timedelta(days=90) if tokens["updated_at"] else None,
    }


# ---------------------------------------------------------------------------
# One-time connect (OAuth + PKCE) - driven from the page's admin section
# ---------------------------------------------------------------------------

def start_login():
    """Returns the Etsy URL to send the admin to. The PKCE verifier is kept
    in the database (not the Streamlit session) because the browser does a
    full page load when Etsy redirects back, which starts a new session."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)

    conn = _require_db()
    try:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM etsy_oauth_pending WHERE created_at < now() - interval '1 day'")
            cur.execute("INSERT INTO etsy_oauth_pending (state, code_verifier) VALUES (%s, %s)", (state, verifier))
        conn.commit()
    finally:
        conn.close()

    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": os.getenv("ETSY_REDIRECT_URI"),
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def parse_redirect(url_or_query):
    """Pulls code/state out of a pasted redirect URL (fallback for when the
    automatic redirect doesn't land back on the page)."""
    q = parse_qs(urlparse(url_or_query.strip()).query or url_or_query.strip().lstrip("?"))
    return (q.get("code") or [None])[0], (q.get("state") or [None])[0], (q.get("error") or [None])[0]


def finish_login(code, state):
    """Swaps the code Etsy redirected back with for tokens, looks up the
    shop, and saves everything for the nightly job."""
    conn = _require_db()
    try:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM etsy_oauth_pending WHERE state = %s RETURNING code_verifier, created_at",
                (state,),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        raise EtsyAuthError("That Etsy approval link was already used or has expired - click Connect Etsy again.")
    verifier, created_at = row
    if datetime.now(timezone.utc) - created_at > PENDING_LOGIN_TTL:
        raise EtsyAuthError("That Etsy approval took too long and expired - click Connect Etsy again.")

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": _client_id(),
        "redirect_uri": os.getenv("ETSY_REDIRECT_URI"),
        "code": code,
        "code_verifier": verifier,
    }, timeout=30)
    if resp.status_code != 200:
        raise EtsyAuthError(f"Etsy refused the login (HTTP {resp.status_code}): {resp.text[:300]}")
    tok = resp.json()

    # The access token is prefixed with the Etsy user id ("12345678.xxxx").
    user_id = int(tok["access_token"].split(".")[0])
    headers = {"x-api-key": _api_key_header(), "Authorization": f"Bearer {tok['access_token']}"}
    me = requests.get(f"{API_BASE}/application/users/me", headers=headers, timeout=30)
    me.raise_for_status()
    shop_id = me.json().get("shop_id")
    if not shop_id:
        raise EtsyAuthError("Connected, but that Etsy account has no shop. Log in to Etsy as the shop owner and try again.")
    shop = requests.get(f"{API_BASE}/application/shops/{shop_id}", headers=headers, timeout=30)
    shop_name = shop.json().get("shop_name") if shop.ok else None

    _save_tokens(tok["access_token"], tok["refresh_token"], tok.get("expires_in"),
                 user_id=user_id, shop_id=shop_id, shop_name=shop_name)
    return shop_name or str(shop_id)


def _refresh(tokens):
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": _client_id(),
        "refresh_token": tokens["refresh"],
    }, timeout=30)
    if resp.status_code != 200:
        raise EtsyAuthError(
            f"Etsy token refresh failed (HTTP {resp.status_code}) - the saved connection has probably "
            "expired. An admin needs to reconnect from the Etsy Orders page."
        )
    tok = resp.json()
    _save_tokens(tok["access_token"], tok.get("refresh_token") or tokens["refresh"], tok.get("expires_in"))
    return {**tokens, "access": tok["access_token"], "refresh": tok.get("refresh_token") or tokens["refresh"],
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in") or 3600))}


# ---------------------------------------------------------------------------
# Authenticated API calls
# ---------------------------------------------------------------------------

def money(m):
    """Etsy Money object {"amount": 1234, "divisor": 100} -> 12.34"""
    if not m:
        return 0.0
    return round((m.get("amount") or 0) / (m.get("divisor") or 100), 2)


class EtsyClient:
    def __init__(self):
        if not is_configured():
            raise EtsyAuthError("Etsy isn't set up - ETSY_API_KEY, ETSY_SHARED_SECRET and ETSY_REDIRECT_URI must be set in Railway.")
        tokens = _load_tokens()
        if not tokens:
            raise EtsyAuthError("Etsy isn't connected yet. An admin needs to connect it from the Etsy Orders page.")
        if tokens["expires_at"] - datetime.now(timezone.utc) < REFRESH_WHEN_LESS_THAN:
            tokens = _refresh(tokens)
        self._tokens = tokens
        self.shop_id = tokens["shop_id"]
        self._session = requests.Session()

    def get(self, path, params=None, _attempt=0):
        if self._tokens["expires_at"] - datetime.now(timezone.utc) < REFRESH_WHEN_LESS_THAN:
            self._tokens = _refresh(self._tokens)
        resp = self._session.get(
            f"{API_BASE}/application{path}",
            params=params,
            headers={"x-api-key": _api_key_header(), "Authorization": f"Bearer {self._tokens['access']}"},
            timeout=60,
        )
        if resp.status_code == 401 and _attempt == 0:
            self._tokens = _refresh(self._tokens)
            return self.get(path, params, _attempt + 1)
        if resp.status_code == 429 and _attempt < 4:
            time.sleep(min(int(resp.headers.get("Retry-After", "2") or 2), 30) + 1)
            return self.get(path, params, _attempt + 1)
        resp.raise_for_status()
        return resp.json()

    def _paged(self, path, params, limit=100):
        out, offset = [], 0
        while True:
            data = self.get(path, {**params, "limit": limit, "offset": offset})
            batch = data.get("results") or []
            out.extend(batch)
            offset += len(batch)
            if not batch or offset >= (data.get("count") or 0):
                return out

    # -- orders ---------------------------------------------------------------

    def receipts(self, start_utc, end_utc):
        """Every receipt (order) created between two UTC datetimes, with its
        line items (transactions) and refunds embedded."""
        return self._paged(f"/shops/{self.shop_id}/receipts", {
            "min_created": int(start_utc.timestamp()),
            "max_created": int(end_utc.timestamp()),
        })

    def receipt_payments(self, receipt_id):
        """Etsy Payments record(s) for a receipt - amount_fees is the payment
        processing fee (adjusted_fees after a refund)."""
        data = self.get(f"/shops/{self.shop_id}/receipts/{receipt_id}/payments")
        return data.get("results") or []

    # -- fees -----------------------------------------------------------------

    def ledger_entries(self, start_utc, end_utc):
        """Payment-account ledger entries (fees, deposits, credits) posted
        between two UTC datetimes. Amounts are in cents; fees are negative."""
        entries = []
        # Etsy caps the window per request, so walk it in 30-day chunks.
        cursor = start_utc
        while cursor < end_utc:
            chunk_end = min(cursor + timedelta(days=30), end_utc)
            entries.extend(self._paged(f"/shops/{self.shop_id}/payment-account/ledger-entries", {
                "min_created": int(cursor.timestamp()),
                "max_created": int(chunk_end.timestamp()),
            }))
            cursor = chunk_end
        seen, unique = set(), []
        for e in entries:
            if e.get("entry_id") not in seen:
                seen.add(e.get("entry_id"))
                unique.append(e)
        return unique

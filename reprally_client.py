"""
Read-only client for the RepRally brand portal (brands.reprally.com) -
Mama's RepRally order numbers (cases per flavor, order counts) for the
morning brief.

RepRally has no public API for brands, so this uses the same calls the
portal's own pages make (found in the browser's Network tab). They're not
officially supported and could change without notice. If this breaks,
sign in at brands.reprally.com in Chrome, open F12 > Network, reload the
Overview page, and compare the calls against the ones below.

AUTH: the portal signs in by POSTing email + password to
birdie.reprally.com/api/v1/loginAccount and gets back a token. There's no
refresh token, so this signs in fresh every run. Set on the Railway
service that sends the morning brief:
    REPRALLY_USERNAME   the RepRally login email
    REPRALLY_PASSWORD   its password
    REPRALLY_BRAND_ID   optional - taken from the login response if unset

WHAT COUNTS: each item sold on RepRally is one case. Only current
products are counted - anything RepRally has marked "ARCHIVED_" (the old
Original Strength line) is left out.
"""
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

LOGIN_URL = "https://birdie.reprally.com/api/v1/loginAccount"
PORTAL = "https://brands.reprally.com"
TZ = ZoneInfo("America/Chicago")

# Display order in the brief. Any other current flavor RepRally reports is
# listed after these.
FLAVORS = ["Strawberry Lemonade", "Classic Lemonade", "Tangerine Cream", "Iced Tea"]


class RepRallyError(RuntimeError):
    pass


def is_configured():
    return bool(os.getenv("REPRALLY_USERNAME") and os.getenv("REPRALLY_PASSWORD"))


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _day_range(first, last):
    """Central-time calendar days -> the portal's startDate/endDate."""
    start = datetime.combine(first, time.min, tzinfo=TZ)
    end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=TZ) - timedelta(milliseconds=1)
    return {"startDate": _iso(start), "endDate": _iso(end)}


class RepRallyClient:
    def __init__(self):
        if not is_configured():
            raise RepRallyError("REPRALLY_USERNAME / REPRALLY_PASSWORD aren't set.")
        self._session = requests.Session()
        self._login()

    def _login(self):
        resp = self._session.post(
            LOGIN_URL,
            json={"username": os.getenv("REPRALLY_USERNAME"), "password": os.getenv("REPRALLY_PASSWORD")},
            headers={"Origin": PORTAL, "Referer": PORTAL + "/"},
            timeout=30,
        )
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or not data.get("auth") or not data.get("token"):
            raise RepRallyError(f"RepRally sign-in failed (HTTP {resp.status_code}) - check REPRALLY_USERNAME / REPRALLY_PASSWORD.")
        self._token = data["token"]
        self.brand_id = os.getenv("REPRALLY_BRAND_ID") or data.get("brandId")
        if not self.brand_id:
            raise RepRallyError("Signed in to RepRally, but the account has no brand.")

    def _get(self, path, params, _retried=False):
        resp = self._session.get(
            f"{PORTAL}/api/brands/{self.brand_id}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._token}", "Referer": PORTAL + "/overview"},
            timeout=30,
        )
        if resp.status_code in (401, 403) and not _retried:
            self._login()
            return self._get(path, params, _retried=True)
        resp.raise_for_status()
        return resp.json()

    def orders(self, first, last):
        """Number of RepRally orders placed between two dates (inclusive)."""
        return int(self._get("/overview", _day_range(first, last)).get("totalOrders") or 0)

    def cases_by_flavor(self, first, last):
        """{flavor: cases} for current products, every FLAVORS entry present
        (0 if none sold)."""
        data = self._get("/top-products", _day_range(first, last))
        out = {f: 0 for f in FLAVORS}
        for sku in data.get("mappedSKUs") or []:
            name = sku.get("name") or ""
            if name.upper().startswith("ARCHIVED"):
                continue
            flavor = name.rsplit(" - ", 1)[-1].strip() if " - " in name else name
            out[flavor] = out.get(flavor, 0) + int(sku.get("totalCases") or 0)
        return out

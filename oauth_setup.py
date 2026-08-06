"""
Run this ONCE per store to connect it to the app.

What it does:
1. Opens your browser to a Lightspeed login/authorize screen for one store.
2. You log in with that store's normal Lightspeed username/password and
   click "Authorize".
3. Lightspeed redirects your browser to your configured redirect_uri with a
   ?code=... in the address bar. Your browser will likely show a "can't
   connect" / SSL error page at that point - that's expected and fine,
   nothing needs to actually load. You just copy the full address bar URL.
4. You paste that URL back into this terminal window.
5. This script pulls the code out of it, exchanges it for an access_token +
   refresh_token, and saves them into stores_config.json.

You will run this 10 times total (once per store), pointing at each
different store_key ("store_1", "store_2", ... "store_10").

BEFORE RUNNING THIS FOR THE FIRST TIME:
1. Go to https://cloud.lightspeedapp.com/oauth/register.php and register one
   application. Whatever Redirect URI you set there (e.g.
   https://localhost:8765) must exactly match "redirect_uri" in
   stores_config.json.
2. Copy the "Client ID" and "Client Secret" it gives you into
   stores_config.json (top-level "client_id" / "client_secret" fields).
3. Then run:  python oauth_setup.py store_1
   ...and repeat for store_2 through store_10, logging into the
   corresponding store's Lightspeed account each time.
"""

import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
import requests

from lightspeed_client import load_config, save_config, TOKEN_URL_TEMPLATE

AUTHORIZE_URL = "https://cloud.lightspeedapp.com/oauth/authorize.php"


def extract_code(pasted_text):
    """Accepts either the full redirect URL or just a bare code, and pulls
    out the authorization code."""
    pasted_text = pasted_text.strip()
    if "code=" in pasted_text:
        parsed = urlparse(pasted_text)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if code:
            return code
    # fall back: assume they pasted just the raw code itself
    return pasted_text


def main():
    if len(sys.argv) != 2:
        print("Usage: python oauth_setup.py store_1   (through store_10)")
        sys.exit(1)

    store_key = sys.argv[1]
    config = load_config()

    if store_key not in config["stores"]:
        print(f"'{store_key}' not found in stores_config.json")
        sys.exit(1)

    client_id = config["client_id"]
    client_secret = config["client_secret"]
    redirect_uri = config["redirect_uri"]

    if "PASTE_YOUR" in client_id:
        print(
            "You need to register an app at "
            "https://cloud.lightspeedapp.com/oauth/register.php and paste "
            "the client_id / client_secret into stores_config.json first."
        )
        sys.exit(1)

    store_name = config["stores"][store_key].get("name", store_key)
    auth_url = (
        f"{AUTHORIZE_URL}?response_type=code&client_id={client_id}"
        f"&redirect_uri={redirect_uri}&scope=employee:all&state=setup"
    )

    print(f"\n=== Connecting {store_name} ({store_key}) ===")
    print(f"Opening your browser. Log in to the {store_name} Lightspeed "
          f"account and click Authorize.")
    print(
        "\nAfter you click Authorize, your browser will likely show an "
        "error page (like 'can't connect' or a security warning) - that is "
        "EXPECTED. Don't worry about the error page itself.\n"
        "Just look at the address bar at the top of your browser, copy the "
        "ENTIRE url shown there, and paste it below.\n"
    )
    webbrowser.open(auth_url)

    print(
        "IMPORTANT: the code expires 60 seconds after you click Authorize, "
        "so copy/paste quickly once you're redirected.\n"
    )
    pasted = input("Paste the full URL from your browser's address bar here, then press Enter:\n> ")
    code = extract_code(pasted)

    print("\nGot authorization code, exchanging for tokens...")

    resp = requests.post(
        TOKEN_URL_TEMPLATE,
        json={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"\n❌ Lightspeed rejected the request (status {resp.status_code}).")
        print("Here's the exact reason it gave:\n")
        print(resp.text)
        print(
            "\nIf it mentions the code being expired/invalid, the code expired "
            "before we could use it (60 second limit) - just run this script "
            "again and move a bit faster on the copy/paste step.\n"
            "If it mentions redirect_uri or client_id/secret, double-check "
            "stores_config.json matches exactly what's registered on "
            "Lightspeed's site."
        )
        sys.exit(1)
    payload = resp.json()

    access_token = payload["access_token"]
    refresh_token = payload["refresh_token"]
    expires_in = int(payload.get("expires_in", 1800))

    # Look up the account ID for this store (needed on every API call)
    acct_resp = requests.get(
        "https://api.lightspeedapp.com/API/V3/Account.json",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    acct_resp.raise_for_status()
    account_id = acct_resp.json()["Account"]["accountID"]

    store_cfg = config["stores"][store_key]
    store_cfg["access_token"] = access_token
    store_cfg["refresh_token"] = refresh_token
    store_cfg["account_id"] = account_id
    store_cfg["token_expires_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
    ).isoformat()

    save_config(config)
    print(f"\n✅ {store_name} connected successfully! (account ID {account_id})")
    print("You can now close this and repeat for the next store, e.g.:")
    print("   python oauth_setup.py store_2\n")


if __name__ == "__main__":
    main()

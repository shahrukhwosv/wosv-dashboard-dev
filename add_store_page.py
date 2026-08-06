"""
Add Store page.

Lets you connect a new Lightspeed store to the dashboard directly from the
browser, instead of running oauth_setup.py in a terminal. Requires a
database to be configured (DATABASE_URL) - otherwise newly-added stores
would be lost the next time the app restarts.

Access is controlled by the login system in auth.py/app.py - no separate
page password needed here anymore.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

import streamlit as st
import requests

from lightspeed_client import load_config, save_config, TOKEN_URL_TEMPLATE
from store_access import grant_access

AUTHORIZE_URL = "https://cloud.lightspeedapp.com/oauth/authorize.php"

st.title("Add a Store")

if not st.session_state.get("is_admin"):
    st.error("Only admin accounts can connect new stores.")
    st.stop()

if not os.getenv("DATABASE_URL"):
    st.error(
        "No database is configured (DATABASE_URL is not set), so a newly "
        "added store would be LOST the next time this app restarts or "
        "redeploys. Set up a database before using this page - see the "
        "note in README.md."
    )
    st.stop()

config = load_config()

if "PASTE_YOUR" in config.get("client_id", ""):
    st.error(
        "No Lightspeed app is registered yet (client_id/client_secret are "
        "still placeholders in the config). Register one at "
        "https://cloud.lightspeedapp.com/oauth/register.php first, then "
        "set client_id/client_secret before adding stores."
    )
    st.stop()


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "store"


def _next_store_key(existing_keys, base_slug):
    if base_slug not in existing_keys:
        return base_slug
    i = 2
    while f"{base_slug}_{i}" in existing_keys:
        i += 1
    return f"{base_slug}_{i}"


def _extract_code(pasted_text):
    pasted_text = pasted_text.strip()
    if "code=" in pasted_text:
        parsed = urlparse(pasted_text)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if code:
            return code
    return pasted_text


# Keep the pending new store's details across the two-step flow (generate
# link -> paste redirect URL) using session state, since Streamlit reruns
# the whole script on every interaction.
if "pending_store_name" not in st.session_state:
    st.session_state.pending_store_name = None
    st.session_state.pending_store_key = None
    st.session_state.pending_avg_ticket_threshold = 75.0

st.subheader("1. Name the store")
store_name = st.text_input(
    "Store name (as you want it to show up in the dashboard)",
    value=st.session_state.pending_store_name or "",
)
avg_ticket_threshold = st.number_input(
    "Average ticket threshold for this store ($)",
    min_value=0.0,
    value=st.session_state.pending_avg_ticket_threshold,
    step=1.0,
)

generate = st.button("Generate connection link", type="primary", disabled=not store_name)

if generate:
    existing_keys = set(config["stores"].keys())
    store_key = _next_store_key(existing_keys, _slugify(store_name))
    st.session_state.pending_store_name = store_name
    st.session_state.pending_store_key = store_key
    st.session_state.pending_avg_ticket_threshold = avg_ticket_threshold

if st.session_state.pending_store_key:
    client_id = config["client_id"]
    redirect_uri = config["redirect_uri"]
    auth_url = (
        f"{AUTHORIZE_URL}?response_type=code&client_id={client_id}"
        f"&redirect_uri={redirect_uri}&scope=employee:all&state=setup"
    )

    st.subheader("2. Connect to Lightspeed")
    st.markdown(
        f"Click the link below, **log in with {st.session_state.pending_store_name}'s "
        f"Lightspeed account**, and click Authorize."
    )
    st.link_button("Open Lightspeed login", auth_url)

    st.markdown(
        "After you click Authorize, your browser will likely show an error "
        "page (like 'can't connect') - that's expected, ignore it. Click "
        "into the address bar, select the whole URL, copy it, and paste it "
        "below.\n\n"
        "**The code expires 60 seconds after you authorize**, so move quickly."
    )

    pasted = st.text_input("Paste the full redirect URL here")
    complete = st.button("Complete connection", disabled=not pasted)

    if complete:
        code = _extract_code(pasted)
        client_secret = config["client_secret"]

        with st.spinner("Exchanging code for access tokens..."):
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
            st.error(f"Lightspeed rejected the request (status {resp.status_code}).")
            st.code(resp.text)
            st.info(
                "If it mentions the code being expired/invalid, the 60-second "
                "window passed - click 'Generate connection link' again and "
                "move faster on the copy/paste step."
            )
        else:
            payload = resp.json()
            access_token = payload["access_token"]
            refresh_token = payload["refresh_token"]
            expires_in = int(payload.get("expires_in", 1800))

            acct_resp = requests.get(
                "https://api.lightspeedapp.com/API/V3/Account.json",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
            acct_resp.raise_for_status()
            account_id = acct_resp.json()["Account"]["accountID"]

            store_key = st.session_state.pending_store_key
            config["stores"][store_key] = {
                "name": st.session_state.pending_store_name,
                "avg_ticket_threshold": st.session_state.pending_avg_ticket_threshold,
                "domain_prefix": "",
                "account_id": account_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_expires_at": (
                    datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
                ).isoformat(),
            }
            save_config(config)
            # Whoever connects a store can immediately see it themselves.
            # Admins can see every store regardless; for non-admin users,
            # this grant is what makes it show up for them - an admin can
            # extend access to other users afterward from Manage Users.
            grant_access(st.session_state.user_id, store_key)

            st.success(
                f"✅ {st.session_state.pending_store_name} connected successfully! "
                f"(store key: {store_key}, account ID: {account_id})"
            )
            st.session_state.pending_store_name = None
            st.session_state.pending_store_key = None
            st.session_state.pending_avg_ticket_threshold = 75.0

st.divider()
st.subheader("All connected stores")
connected = {k: v for k, v in config["stores"].items() if v.get("refresh_token")}
if connected:
    for key, store in connected.items():
        col1, col2 = st.columns([4, 1])
        with col1:
            st.write(f"- **{store.get('name', key)}** ({key})")
        with col2:
            confirm_key = f"confirm_delete_store_{key}"
            if st.session_state.get(confirm_key):
                if st.button("Confirm delete", key=f"confirm_del_btn_{key}", type="primary"):
                    del config["stores"][key]
                    save_config(config)
                    st.success(f"Deleted {store.get('name', key)} ({key}).")
                    st.session_state.pop(confirm_key, None)
                    st.rerun()
            else:
                if st.button("Delete", key=f"del_btn_{key}"):
                    st.session_state[confirm_key] = True
                    st.rerun()
else:
    st.write("No stores connected yet.")

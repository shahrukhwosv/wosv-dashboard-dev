# Employee Commission Report Generator

A local tool that connects to all 10 of your Lightspeed R-Series store
accounts, and lets you generate an employee commission report for any date
range by clicking a store name and picking dates — no spreadsheets, no
manual login per store after the initial setup.

## How it works

- **stores_config.json** — holds the connection info for each of your 10
  stores (filled in automatically during setup, one time).
- **rules_config.json** — your commission rules, in plain English-ish
  settings. Edit amounts/thresholds here any time, no coding needed.
- **oauth_setup.py** — run once per store to connect it (10 times total,
  ever — unless a store's connection is ever revoked).
- **app.py** — the actual report-generator app you'll use day to day.
- **lightspeed_client.py / commission_engine.py** — the underlying logic
  (you shouldn't need to touch these unless a rule type changes).

## One-time setup

### Step 1 — Install Python packages

Open a terminal in this folder and run:

```
pip install -r requirements.txt
```

### Step 2 — Register one app with Lightspeed

1. Go to https://cloud.lightspeedapp.com/oauth/register.php
2. Register a new application. Use:
   - **Redirect URI:** `http://localhost:8765/callback`
   - **Scopes:** read access to Sales and Employees (no write access needed)
3. It will give you a **Client ID** and **Client Secret**. Open
   `stores_config.json` and paste them into the `"client_id"` and
   `"client_secret"` fields at the top.

This is a one-time step — the same app connects to all 10 stores.

### Step 3 — Connect each store (10 times, once each)

For each store, run:

```
python oauth_setup.py store_1
```

Your browser will open. Log in with **that store's** Lightspeed
username/password and click "Authorize." The script will automatically
capture the connection and save it.

Repeat for `store_2` through `store_10`, logging into the corresponding
store's account each time. (Tip: while you're connecting each one, also open
`stores_config.json` and rename `"name": "Store 1"` etc. to your real store
names, and set each store's `avg_ticket_threshold` to the correct dollar
value for that store.)

### Step 4 — Confirm the promo-detection field (do this once)

Commission rules #1 and #2 (big-sale bonus and average-ticket bonus) will
work immediately since they're based on the sale total, which is standard.
Rule #3 (promo conversion) depends on exactly how your specific promo shows
up in the API data, which can vary. After connecting at least one store,
run:

```
python inspect_sample.py store_1
```

This prints one real sale's full data. Paste that output back to Claude and
we'll adjust the matching logic in `lightspeed_client.py` so it correctly
detects your specific promo every time.

### Step 5 — Set your commission rules

Open `rules_config.json`. The three rules from your examples are already set
up:

- `big_sale_bonus` — $5 for every sale over $100
- `average_ticket_bonus` — $50 if average ticket exceeds the store's
  threshold (set per-store in `stores_config.json`)
- `promo_conversion_bonus` — $1 every time a specific promo is applied
  (set the promo name to match in `"promo_name_match"`)

You can change any dollar amount or threshold directly in this file. To
disable a rule temporarily, set `"enabled": false`. To add a brand new rule
type down the road, let Claude know what the rule is and we'll add it to
`commission_engine.py`.

## Running the app (day to day)

```
streamlit run app.py
```

This opens a browser tab. Pick a store (or "All connected stores"), pick
your biweekly date range, click **Generate Report**. You'll see a table
on-screen with the full breakdown per employee, plus a button to download it
as an Excel file for payroll.

## Notes

- Access tokens refresh automatically — you never have to log in again after
  the one-time setup, unless you explicitly revoke access in Lightspeed.
- If a store's connection ever breaks (e.g. password changed, access
  revoked), just re-run `python oauth_setup.py store_X` for that one store.
- All data stays on your own computer — nothing is uploaded anywhere except
  directly between your machine and Lightspeed's API.

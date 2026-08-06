"""
Google Sheets client for the Touch Tell invoice sheet.

Reads the block-per-store layout (3 columns per store: Invoice #, Amount,
Status) and can write "Ready to Pay" back into the Status column for
specific rows.

AUTH:
Uses a Google service account. Locally, put the service account's JSON key
file at service_account.json next to this file. On Railway (or wherever
this is deployed), set an environment variable GOOGLE_SERVICE_ACCOUNT_JSON
containing the full JSON key contents, same pattern as STORES_CONFIG_JSON
in lightspeed_client.py.

Before this will work you must:
  1. Create a Google Cloud project, enable the "Google Sheets API".
  2. Create a service account, generate a JSON key.
  3. Share the Touch Tell Google Sheet with the service account's email
     address (found in the JSON key as "client_email"), Editor access,
     since we need to write "Ready to Pay" back into it.
"""
import json
import os

import gspread
from google.oauth2.service_account import Credentials

SERVICE_ACCOUNT_PATH = "service_account.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _load_credentials():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    return Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=SCOPES)


def get_worksheet(sheet_id, worksheet_name):
    creds = _load_credentials()
    client = gspread.authorize(creds)
    sh = client.open_by_key(sheet_id)
    return sh.worksheet(worksheet_name)


def read_invoice_rows(sheet_id, worksheet_name, store_sheet_to_key):
    """
    Reads the block-per-store layout and returns a flat list of dicts, one
    per invoice row, for every store block found on row 1 (regardless of
    whether we have a Lightspeed connection mapped for it yet - unmapped
    stores are flagged separately so nothing is silently skipped).

    Row 1: store names, one per 3-column block.
    Row 2: "Ready to Pay Balance" / balance / "Status" header row - skipped.
    Row 3+: invoice_number, amount, status

    Returns list of:
      {
        "store_sheet_name": str,
        "store_key": str or None,   # None if not in store_sheet_to_key
        "row": int,                 # 1-indexed sheet row, for writing back
        "invoice_col": int,         # 1-indexed column of the invoice # cell
        "status_col": int,          # 1-indexed column of the status cell
        "invoice_number": value,
        "amount": float or None,
        "status": str or None,
      }
    """
    ws = get_worksheet(sheet_id, worksheet_name)
    all_values = ws.get_all_values()

    if len(all_values) < 3:
        return []

    header_row = all_values[0]
    rows_out = []

    for col_idx in range(0, len(header_row), 3):
        store_name = header_row[col_idx].strip() if col_idx < len(header_row) else ""
        if not store_name:
            continue
        store_key = store_sheet_to_key.get(store_name)

        for sheet_row_idx in range(2, len(all_values)):  # 0-indexed, row 3+
            row = all_values[sheet_row_idx]
            if col_idx >= len(row):
                continue
            invoice_raw = row[col_idx].strip() if col_idx < len(row) else ""
            amount_raw = row[col_idx + 1].strip() if col_idx + 1 < len(row) else ""
            status_raw = row[col_idx + 2].strip() if col_idx + 2 < len(row) else ""

            if not invoice_raw:
                continue

            try:
                amount = float(amount_raw.replace("$", "").replace(",", "").strip())
            except (TypeError, ValueError):
                amount = None

            rows_out.append({
                "store_sheet_name": store_name,
                "store_key": store_key,
                "row": sheet_row_idx + 1,  # back to 1-indexed for gspread
                "invoice_col": col_idx + 1,
                "status_col": col_idx + 3,
                "invoice_number": invoice_raw,
                "amount": amount,
                "status": status_raw or None,
            })

    return rows_out


def write_status(sheet_id, worksheet_name, cells_to_update, status_value):
    """
    cells_to_update: list of (row, status_col) tuples to set to status_value.
    Batched into a single API call.
    """
    if not cells_to_update:
        return
    ws = get_worksheet(sheet_id, worksheet_name)
    updates = [
        {"range": gspread.utils.rowcol_to_a1(row, col), "values": [[status_value]]}
        for row, col in cells_to_update
    ]
    ws.batch_update(updates)


def write_ready_to_pay(sheet_id, worksheet_name, cells_to_update):
    write_status(sheet_id, worksheet_name, cells_to_update, "Ready to Pay")


def write_paid(sheet_id, worksheet_name, cells_to_update):
    write_status(sheet_id, worksheet_name, cells_to_update, "Paid")

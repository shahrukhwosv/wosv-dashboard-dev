"""
Parses a ValorPay "batch out report" CSV export into a clean list of
transactions we can match against Lightspeed sales.
"""

import pandas as pd
from datetime import datetime


def _parse_money(value):
    """Converts '$24.75' or '24.75' or '' into a float."""
    if value is None:
        return 0.0
    s = str(value).replace("$", "").replace(",", "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_valor_csv(file_obj):
    """
    file_obj: an uploaded file object (e.g. from st.file_uploader)

    Returns a list of dicts:
        {timestamp (datetime), base_amount, differential, net_amount,
         tx_type, card_scheme, masked_card, approval_code, transaction_id,
         store_name}

    Only rows with TX TYPE == 'CREDIT SALE' are included by default (refunds/
    voids are returned separately in case you want to review them, but they
    aren't matched against Lightspeed sales).
    """
    df = pd.read_csv(file_obj)
    df.columns = [c.strip() for c in df.columns]

    sales = []
    other_tx_types = []

    for _, row in df.iterrows():
        tx_type = str(row.get("TX TYPE", "")).strip()

        try:
            timestamp = datetime.strptime(
                str(row.get("TRANSACTION DATE", "")).strip(), "%m/%d/%Y %H:%M"
            )
        except ValueError:
            continue  # skip rows with unparseable dates (e.g. stray blank rows)

        record = {
            "timestamp": timestamp,
            "base_amount": _parse_money(row.get("BASE AMOUNT")),
            "differential": _parse_money(row.get("Differential")),
            "net_amount": _parse_money(row.get("NET AMOUNT")),
            "tx_type": tx_type,
            "card_scheme": str(row.get("CARD SCHEME", "")).strip(),
            "masked_card": str(row.get("MASKEDCARD NO", "")).strip(),
            "approval_code": str(row.get("Approval Code", "")).strip(),
            "transaction_id": str(row.get("TRANSACTION ID", "")).strip(),
            "store_name": str(row.get("STORE NAME", "")).strip(),
        }

        if tx_type.upper() == "CREDIT SALE":
            sales.append(record)
        else:
            other_tx_types.append(record)

    return sales, other_tx_types

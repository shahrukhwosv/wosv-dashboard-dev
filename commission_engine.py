"""
Commission calculation engine.

Takes a list of normalized transactions (see lightspeed_client.normalize_sale)
plus the rules defined in rules_config.json, and produces a per-employee
breakdown showing exactly how each rule contributed to their total.

Rules always STACK (all enabled rules that apply are added together), per
your instructions. Adding a new rule type later just means adding a new
`elif rule["type"] == "...":` block below - no other file needs to change.
"""

import json
import os
from collections import defaultdict
import pandas as pd


def load_rules(path="rules_config.json"):
    with open(path, "r") as f:
        return json.load(f)["rules"]


def load_stores_meta(path="stores_config.json"):
    config_json = os.getenv("STORES_CONFIG_JSON")

    if config_json:
        config = json.loads(config_json)
    else:
        with open(path, "r") as f:
            config = json.load(f)

    return {
        store_key: {
            "name": store.get("name", store_key),
            "avg_ticket_threshold": store.get("avg_ticket_threshold", 0),
        }
        for store_key, store in config.get("stores", {}).items()
    }


def calculate_commissions(transactions, rules, stores_meta):
    """
    transactions: list of dicts from lightspeed_client.normalize_sale()
    rules: list of rule dicts from rules_config.json
    stores_meta: dict of store_key -> store config (for avg_ticket_threshold)

    Returns a pandas DataFrame, one row per employee, with a column per rule
    plus a 'total_commission' column.
    """
    # Group transactions by employee
    by_employee = defaultdict(list)
    for tx in transactions:
        by_employee[(tx["store"], tx["employee_id"], tx["employee_name"])].append(tx)

    rows = []
    for (store, emp_id, emp_name), tx_list in by_employee.items():
        row = {
            "store": stores_meta.get(store, {}).get("name", store),
            "employee_id": emp_id,
            "employee_name": emp_name,
            "transaction_count": len(tx_list),
        }
        total_commission = 0.0

        for rule in rules:
            if not rule.get("enabled", True):
                continue

            rule_id = rule["id"]
            payout = 0.0

            if rule["type"] == "per_transaction_threshold":
                threshold = float(rule["threshold_amount"])
                amount = float(rule["payout_amount"])

                # Pay once for every complete threshold reached on each sale,
                # using the after-discount, pre-tax merchandise subtotal.
                # Example: $350 net subtotal at a $100 threshold = 3 payouts.
                payout_units = sum(
                    int(float(tx.get("net_subtotal", 0) or 0) // threshold)
                    for tx in tx_list
                )
                payout = payout_units * amount
                row[f"{rule_id}_count"] = payout_units

            elif rule["type"] == "average_ticket_threshold":
                store_threshold = stores_meta.get(store, {}).get(
                    "avg_ticket_threshold", float("inf")
                )
                avg_ticket = (
                    sum(tx["total"] for tx in tx_list) / len(tx_list)
                    if tx_list
                    else 0
                )
                row["average_ticket"] = round(avg_ticket, 2)
                if avg_ticket > store_threshold:
                    payout = rule["payout_amount"]

            elif rule["type"] == "promo_applied":
                amount = rule["payout_amount"]
                names = rule["promo_name_match"]
                if isinstance(names, str):
                    names = [names]
                count_mode = rule.get("count_mode", "transaction")
                match_key = (
                    "promo_matches"
                    if count_mode == "line"
                    else "promo_transactions"
                )
                promo_count = sum(
                    tx.get(match_key, {}).get(name, 0)
                    for tx in tx_list
                    for name in names
                )
                conversions_per_payout = float(rule.get("conversions_per_payout", 1) or 1)
                # Divide the conversion count only once. For the PUFF rule,
                # 16 conversions / 2 conversions per payout * $1 = $8.
                payout = round((promo_count / conversions_per_payout) * amount, 2)
                row[f"{rule_id}_count"] = promo_count

            row[f"{rule_id}_payout"] = round(payout, 2)
            total_commission += payout

        row["total_commission"] = round(total_commission, 2)
        rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(
            ["store", "total_commission"], ascending=[True, False]
        ).reset_index(drop=True)
    return df

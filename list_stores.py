"""
Run via Railway's Console tab to see every connected store, including
duplicates, with their exact store_key (needed for delete_store.py).

    python list_stores.py
"""

from lightspeed_client import load_config

config = load_config()
stores = config.get("stores", {})

if not stores:
    print("No stores in config.")
else:
    print(f"{len(stores)} store(s):\n")
    for key, store in stores.items():
        has_token = "yes" if store.get("refresh_token") else "NO TOKEN"
        print(
            f"  key={key!r:20} name={store.get('name', ''):20} "
            f"account_id={store.get('account_id', ''):10} "
            f"refresh_token={has_token} "
            f"expires={store.get('token_expires_at', 'n/a')}"
        )

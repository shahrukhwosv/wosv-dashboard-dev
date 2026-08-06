"""
Run via Railway's Console tab to delete one store by its exact store_key
(get the key from list_stores.py first).

    python delete_store.py norman_2
"""

import sys

from lightspeed_client import load_config, save_config


def main():
    if len(sys.argv) != 2:
        print("Usage: python delete_store.py <store_key>")
        sys.exit(1)

    store_key = sys.argv[1]
    config = load_config()

    if store_key not in config["stores"]:
        print(f"'{store_key}' not found. Run list_stores.py to see valid keys.")
        sys.exit(1)

    name = config["stores"][store_key].get("name", store_key)
    del config["stores"][store_key]
    save_config(config)
    print(f"✅ Deleted '{name}' ({store_key}).")


if __name__ == "__main__":
    main()

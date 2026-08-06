"""
Run this ONCE to create your first admin account (solves the chicken-and-
egg problem: Manage Users requires being an admin, but no users exist yet).

Run it from Railway's Console tab (not your local terminal - it needs to
run where DATABASE_URL is actually set):

    python bootstrap_admin.py yourusername yourpassword

After this, log into the dashboard normally with that username/password,
and use the Manage Users page for every account after this one.
"""

import sys

from auth import create_user


def main():
    if len(sys.argv) != 3:
        print("Usage: python bootstrap_admin.py <username> <password>")
        sys.exit(1)

    username, password = sys.argv[1], sys.argv[2]
    try:
        create_user(username, password, is_admin=True)
        print(f"✅ Admin account '{username}' created. Log in with it on the dashboard.")
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

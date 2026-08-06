#!/bin/bash
cd "$(dirname "$0")"
echo "Which store are you connecting? Enter a number 1-10, then press Enter:"
read STORE_NUM
python3 oauth_setup.py "store_$STORE_NUM"
echo ""
echo "Press Enter to close this window..."
read

#!/bin/bash
cd "$(dirname "$0")"
echo "Installing required packages... this may take a minute."
python3 -m pip install -r requirements.txt
echo ""
echo "Done! You can close this window now."
echo "Press Enter to close..."
read

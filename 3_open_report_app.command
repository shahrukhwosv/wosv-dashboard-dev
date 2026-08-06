#!/bin/bash
cd "$(dirname "$0")"
echo "Starting the Commission Report app... a browser tab will open shortly."
python3 -m streamlit run app.py

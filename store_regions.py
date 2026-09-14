"""
Regional store groupings - North/South, matched by store display name
(not store_key, since key numbering doesn't reflect any north/south
grouping).

Pulled out into its own plain module (no Streamlit calls, no side
effects) so it can be safely imported by more than one page.
pace_calculator_page.py originally defined these locally - importing a
page script directly would re-run all of its top-level Streamlit code
(st.title(), the password gate, etc.) as a side effect, so this needed
to live somewhere side-effect-free instead once a second page
(dashboard_page.py's Monthly Sales Trend presets) needed the same
groupings.

NOTE: the config names one of these stores "Greenville" (not "Lower
Greenville") - matched accordingly below.
"""

NORTH_STORES = {"Aubrey", "Rowlett", "Princeton", "Frisco", "Liquor Depot"}
SOUTH_STORES = {"Oak Lawn", "Greenville", "West Greenville", "Lovers", "Hillcrest"}

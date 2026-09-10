"""
lookups.py

Small reference tables (categories, vendors, tax categories, payment
types, employees) rarely change and are cheap to pull in full. Fetched
once per report run and used to resolve IDs to human-readable names when
building the reports, instead of relying on nested load_relations.

Ported from the standalone Lightspeed reports tool to use this app's
existing multi-store lightspeed_client.py (fetch_all) instead of its own
separate single-account client. Also drops the original's shop/shop_name
lookups entirely - that tool filtered by shopID because ONE Lightspeed
account could contain multiple shops; here, each store is already its own
separate Lightspeed account (see lightspeed_client.py's docstring), so
there's no "shop within an account" concept left to resolve.
"""

import lightspeed_client as ls


def build_lookups(config, store_key):
    lookups = {}

    categories = ls.fetch_all(config, store_key, "Category.json")
    lookups["category"] = {
        str(c["categoryID"]): c.get("fullPathName") or c.get("name", "")
        for c in categories
    }

    manufacturers = ls.fetch_all(config, store_key, "Manufacturer.json")
    lookups["manufacturer"] = {str(m["manufacturerID"]): m.get("name", "") for m in manufacturers}

    vendors = ls.fetch_all(config, store_key, "Vendor.json")
    lookups["vendor"] = {str(v["vendorID"]): v.get("name", "") for v in vendors}

    tax_categories = ls.fetch_all(config, store_key, "TaxCategory.json")
    lookups["tax_category"] = {str(t["taxCategoryID"]): t.get("name", "") for t in tax_categories}

    payment_types = ls.fetch_all(config, store_key, "PaymentType.json")
    lookups["payment_type"] = {str(p["paymentTypeID"]): p.get("name", "") for p in payment_types}

    employees = ls.fetch_all(config, store_key, "Employee.json")
    lookups["employee"] = {
        str(e["employeeID"]): f"{e.get('firstName', '')} {e.get('lastName', '')}".strip()
        for e in employees
    }

    return lookups

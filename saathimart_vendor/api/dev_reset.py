"""
Vendor-site dev reset — wipes SaathiMart/vendor-app transactional data
while keeping the ERPNext masters (Company, Chart of Accounts, Warehouses,
existing Items), users, and Vendor Config identity.

Usage (on any vendor site):
    bench --site vendor1.localhost execute saathimart_vendor.api.dev_reset.reset_vendor_data
"""
import frappe

# vendor-app transactional tables, children first
VENDOR_APP_TABLES = [
    "Vendor Order Item",
    "Vendor Order",
    "Sync Outbox",
]

# Product Mapping is kept only if its ERPNext item still exists; wiped rows
# are recreated by the catalog sync anyway.
ERP_GLUE_TABLES = [
    "Product Mapping",
]

# ERPNext docs the vendor app created for hub orders (transactional only).
ERP_TRANSACTIONAL = [
    "GL Entry",
    "Payment Entry Reference",  # child rows of Payment Entry
    "Payment Entry",
    "Sales Invoice Item",
    "Sales Invoice",
    "Delivery Note Item",
    "Delivery Note",
    "Sales Order Item",
    "Sales Order",
]


def reset_vendor_data(purge_erp=False):
    """Wipe vendor-app transactional data (+ optionally ERPNext txn docs)."""
    print(f"Resetting vendor data on {frappe.local.site}...")

    for dt in VENDOR_APP_TABLES + ERP_GLUE_TABLES:
        try:
            n = frappe.db.count(dt)
            if n:
                frappe.db.sql(f"DELETE FROM `tab{dt}`")  # nosemgrep
                print(f"  {dt}: {n} deleted")
        except Exception as e:
            print(f"  {dt}: SKIP ({str(e)[:60]})")

    if purge_erp:
        for dt in ERP_TRANSACTIONAL:
            try:
                n = frappe.db.count(dt)
                if n:
                    frappe.db.sql(f"DELETE FROM `tab{dt}`")  # nosemgrep
                    print(f"  {dt}: {n} deleted")
            except Exception as e:
                print(f"  {dt}: SKIP ({str(e)[:60]})")

    frappe.db.commit()
    print("Vendor reset complete.")
    return {"ok": True}


def wipe_site_erp(purge_erp=True):
    """Convenience alias: reset everything including ERPNext txn docs."""
    return reset_vendor_data(purge_erp=True)

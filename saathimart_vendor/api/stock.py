"""
Vendor-side stock API — returns actual ERPNext Bin quantities for
reconciliation with the hub's Vendor Stock records.
"""
import frappe
from frappe import _
from frappe.utils import flt


@frappe.whitelist(allow_guest=True)
def get_stock_qty(product=None, warehouse=None):
    """Return actual available qty from ERPNext Bin for a product/warehouse.

    Called by the hub during stock reconciliation to verify sync accuracy.
    """
    from saathimart_vendor.api.receive import _verify_hub_secret, _verify_timestamp
    from saathimart_vendor.utils import get_config, get_mapping

    _verify_hub_secret()
    _verify_timestamp()

    if not product:
        frappe.throw(_("product is required"))

    config = get_config()
    if not config:
        frappe.throw(_("Vendor Config not set up"))

    # Resolve the ERPNext item_code from hub product ID
    mapping = get_mapping(product)
    if not mapping or not mapping.item_code:
        # Try barcode lookup
        mapping_by_barcode = frappe.db.get_value(
            "Product Mapping",
            {"hub_product_id": product, "vendor": config.vendor_id},
            ["item_code", "barcode"],
            as_dict=True,
        )
        if mapping_by_barcode and mapping_by_barcode.item_code:
            item_code = mapping_by_barcode.item_code
        else:
            return {"qty": 0, "resolved": False}
    else:
        item_code = mapping.item_code

    # Determine warehouse
    wh = warehouse or config.default_warehouse
    if warehouse and warehouse != "default":
        # Look up mapped ERPNext warehouse from our warehouse table
        for wh_row in (config.warehouses or []):
            if wh_row.warehouse_name == warehouse and wh_row.erpnext_warehouse:
                wh = wh_row.erpnext_warehouse
                break

    if not wh:
        return {"qty": 0, "resolved": True, "item_code": item_code}

    # Get actual qty from ERPNext Bin
    actual_qty = frappe.db.get_value(
        "Bin",
        {"item_code": item_code, "warehouse": wh},
        "actual_qty",
    ) or 0

    reserved_qty = frappe.db.get_value(
        "Bin",
        {"item_code": item_code, "warehouse": wh},
        "reserved_qty",
    ) or 0

    return {
        "qty": flt(actual_qty) + flt(reserved_qty),
        "actual_qty": flt(actual_qty),
        "reserved_qty": flt(reserved_qty),
        "item_code": item_code,
        "warehouse": wh,
        "resolved": True,
    }

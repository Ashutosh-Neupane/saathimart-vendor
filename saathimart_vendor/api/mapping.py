import csv
import io
import frappe
from frappe import _
from frappe.utils import get_url
from saathimart_vendor.utils import get_config, hub_get, hub_post, build_stock_payload, _get_base_qty


@frappe.whitelist()
def lookup_barcode(barcode):
    """Scan a barcode → ask hub what product it is → return details."""
    config = get_config()
    if not config:
        frappe.throw(_("Vendor Config not configured"))
    result = hub_get(config, "saathimart.api.products.lookup_by_barcode",
                     {"barcode": barcode})
    if not result:
        return {"found": False, "barcode": barcode}
    return {"found": True, "barcode": barcode, **result}


@frappe.whitelist()
def sync_vendor_stock():
    """
    Push the vendor's current stock for all mapped products to the hub.
    Called by the vendor's warehouse staff after stock-taking or receiving.
    """
    config = get_config()
    if not config:
        frappe.throw(_("Vendor Config not configured"))

    mappings = frappe.get_list(
        "Product Mapping",
        filters={"is_active": 1, "sync_status": "Mapped"},
        fields=["name", "barcode", "item_code", "hub_product_id"],
    )
    pushed = 0
    errors = []

    for m in mappings:
        try:
            actual_qty = frappe.db.get_value(
                "Bin",
                {"item_code": m.item_code, "warehouse": config.default_warehouse},
                "actual_qty",
            ) or 0.0

            base_qty = _get_base_qty(m.item_code)

            payload = build_stock_payload(
                mapping=m,
                qty_change=actual_qty,
                voucher_type="Stock Sync",
                voucher_no="",
                source_site=get_url(),
                remarks=f"Vendor stock sync for {m.item_code}",
                base_qty=base_qty,
            )

            ok, msg = hub_post(
                config,
                "saathimart.api.stock.apply_vendor_stock_event",
                {
                    "event": "stock.receipt",
                    "payload": payload,
                },
            )
            if ok:
                pushed += 1
            else:
                errors.append(f"{m.item_code}: {msg}")
        except Exception as e:
            errors.append(f"{m.item_code}: {str(e)[:100]}")

    frappe.db.commit()
    return {"pushed": pushed, "errors": errors}


@frappe.whitelist()
def sync_vendor_location():
    """
    Push the vendor's location (lat/lng/radius) to the hub.
    Called when the vendor updates their delivery location settings.
    """
    config = get_config()
    if not config:
        frappe.throw(_("Vendor Config not configured"))

    ok, msg = hub_post(
        config,
        "saathimart.api.location.update_vendor_location",
        {
            "vendor_id": config.vendor_id,
            "lat": config.lat,
            "lng": config.lng,
            "service_radius_km": config.service_radius_km,
            "address": config.address or "",
        },
    )
    if not ok:
        frappe.throw(_("Failed to sync location: {0}").format(msg))
    return {"ok": True, "message": _("Location synced with hub")}


@frappe.whitelist()
def bulk_import(csv_content):
    """
    CSV format: item_code,barcode
    Creates Product Mapping rows and syncs each with hub.
    Returns summary: {created, skipped, errors}
    """
    config = get_config()
    if not config:
        frappe.throw(_("Vendor Config not configured"))

    created = skipped = 0
    errors = []

    reader = csv.DictReader(io.StringIO(csv_content))
    for i, row in enumerate(reader, start=2):
        item_code = (row.get("item_code") or "").strip()
        barcode   = (row.get("barcode") or "").strip()
        if not item_code or not barcode:
            errors.append(f"Row {i}: missing item_code or barcode")
            continue
        if not frappe.db.exists("Item", item_code):
            errors.append(f"Row {i}: item_code {item_code} not found in ERPNext")
            continue
        if frappe.db.exists("Product Mapping", {"barcode": barcode}):
            skipped += 1
            continue

        hub_result = hub_get(config, "saathimart.api.products.lookup_by_barcode",
                             {"barcode": barcode})

        doc = frappe.new_doc("Product Mapping")
        doc.barcode    = barcode
        doc.item_code  = item_code
        if hub_result:
            doc.hub_product_id = hub_result.get("name", "")
            doc.hub_sku        = hub_result.get("sku", "")
            doc.sync_status    = "Mapped"
            doc.last_synced    = frappe.utils.now_datetime()
        else:
            doc.sync_status = "Unmapped"
            doc.sync_error  = f"Barcode {barcode} not found on hub"
        try:
            doc.insert(ignore_permissions=True)
            created += 1
        except Exception as e:
            errors.append(f"Row {i}: {str(e)[:100]}")

    frappe.db.commit()
    return {"created": created, "skipped": skipped, "errors": errors}


@frappe.whitelist()
def sync_all_unmapped():
    """Re-try hub lookup for all Unmapped Product Mapping rows."""
    config = get_config()
    if not config:
        frappe.throw(_("Vendor Config not configured"))

    unmapped = frappe.get_list(
        "Product Mapping",
        filters={"sync_status": ["in", ["Unmapped", "Error"]], "is_active": 1},
        fields=["name", "barcode"],
    )
    fixed = failed = 0
    for row in unmapped:
        result = hub_get(config, "saathimart.api.products.lookup_by_barcode",
                         {"barcode": row.barcode})
        if result:
            frappe.db.set_value("Product Mapping", row.name, {
                "hub_product_id": result.get("name", ""),
                "hub_sku":        result.get("sku", ""),
                "sync_status":    "Mapped",
                "sync_error":     "",
                "last_synced":    frappe.utils.now_datetime(),
            })
            fixed += 1
        else:
            failed += 1

    frappe.db.commit()
    return {"fixed": fixed, "still_unmapped": failed}

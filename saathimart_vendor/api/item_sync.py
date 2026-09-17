"""Item → SaathiMart sync — the code behind the ERPNext Item "Sync to
Saathi" button (see item.js + hooks doctype_js).

One click does everything the vendor needs to become sellable on the
marketplace:
  1. barcode lookup against the hub (matches an existing product);
  2. Product Mapping row on this site (idempotent on barcode);
  3. hub-side intake (saathimart.api.vendor_item_intake.register_item):
     upserts Product / Vendor Listing (with this vendor's price) / Vendor
     Stock / Vendor Barcode Index in one signed call;
  4. stock truth pushed as a real stock.receipt event through the outbox so
     the hub's Vendor Stock row carries the ERPNext Bin quantity.

Runs synchronously on the button call so the result lands even on dev
containers with no RQ workers; the outbox row is still the durable transport
for the stock delta.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, get_url, now_datetime

from saathimart_vendor.utils import (
    get_config,
    hub_post,
)


def _first_barcode(item_doc) -> str:
    for row in (item_doc.barcodes or []):
        if row.barcode:
            return row.barcode
    return ""


def _selling_price(item_code: str) -> float:
    rate = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "selling": 1},
        "price_list_rate",
    )
    return flt(rate or 0)


def _current_qty(item_code: str, warehouse: str) -> float:
    if not warehouse:
        return 0.0
    qty = frappe.db.get_value(
        "Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
    )
    return flt(qty or 0)


@frappe.whitelist()
def sync_item_to_saathi(item_code: str) -> dict:
    """Whitelisted target of the Item form button. Fully syncs one item."""
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item {0} not found").format(item_code))

    config = get_config()
    if not config:
        frappe.throw(_("Vendor Config not configured — connect this site to SaathiMart first"))
    if not config.sync_enabled:
        frappe.throw(_("Sync is disabled in Vendor Config"))

    item = frappe.get_doc("Item", item_code)
    barcode = _first_barcode(item)
    if not barcode:
        frappe.throw(_(
            "Item {0} has no barcode. Add an Item Barcode row first — "
            "the barcode is what matches (or creates) the hub product."
        ).format(item_code))

    price = _selling_price(item_code)
    qty = _current_qty(item_code, config.default_warehouse)

    # 1-2. Local Product Mapping (idempotent on barcode+vendor).
    mapping_name = frappe.db.get_value(
        "Product Mapping", {"barcode": barcode, "vendor": config.vendor_id}, "name"
    )
    hub_result = None
    if not mapping_name:
        from saathimart_vendor.api.mapping import lookup_barcode
        try:
            hub_result = lookup_barcode(barcode)
        except Exception:
            hub_result = None

        doc = frappe.new_doc("Product Mapping")
        doc.barcode = barcode
        doc.item_code = item_code
        doc.item_name = item.item_name
        doc.vendor = config.vendor_id
        if hub_result and hub_result.get("found") and hub_result.get("name"):
            doc.hub_product_id = hub_result["name"]
            doc.sync_status = "Mapped"
            doc.last_synced = now_datetime()
        else:
            doc.sync_status = "Unmapped"
        doc.insert(ignore_permissions=True)
        mapping_name = doc.name
    else:
        hub_product_id = frappe.db.get_value("Product Mapping", mapping_name, "hub_product_id")
        if hub_product_id:
            hub_result = {"found": True, "name": hub_product_id}

    # 3. Hub-side intake — Product/Listing/Stock/BarcodeIndex upsert.
    ok, msg = hub_post(
        config,
        "saathimart.api.vendor_item_intake.register_item",
        {
            "item_code": item_code,
            "item_name": item.item_name,
            "barcode": barcode,
            "price": price,
            "qty": qty,
            "category": item.item_group or "",
            "brand": item.brand or "",
            "description": item.description or "",
            "uom": item.stock_uom or "",
        },
    )
    if not ok:
        frappe.throw(_("Hub sync failed: {0}").format(msg))

    # Keep the local mapping pointed at the (possibly brand-new) hub product.
    product = (msg or {}).get("product") if isinstance(msg, dict) else None
    if product:
        frappe.db.set_value("Product Mapping", mapping_name, {
            "hub_product_id": product,
            "sync_status": "Mapped",
            "last_synced": now_datetime(),
        })

    # NOTE: no separate stock push here. The hub intake applies the delta
    # itself (see vendor_item_intake.register_item) — pushing the full qty
    # again through the outbox would double-apply it (the stock pipeline is
    # delta-based and each event carries a fresh event_id, so the two pushes
    # are NOT deduped against each other).

    frappe.db.commit()

    listing = (msg or {}).get("listing") if isinstance(msg, dict) else None
    created = (msg or {}).get("listing_created") if isinstance(msg, dict) else None
    # `product` is what the hub's intake response just returned (set above);
    # fall back to whatever this mapping already had on file (e.g. an older
    # mapping reused because this exact barcode+vendor pair already existed)
    # rather than crashing — `mapping` was never a loaded document here,
    # only `mapping_name` (its docname) was.
    product_ref = product or frappe.db.get_value("Product Mapping", mapping_name, "hub_product_id") or ""
    return {
        "ok": True,
        "item_code": item_code,
        "barcode": barcode,
        "hub_product": product_ref,
        "product_url": f"{config.hub_url}/app/product/{product_ref}" if product_ref else "",
        "listing": listing,
        "listing_created": bool(created),
        "price": price,
        "qty": qty,
        "message": (
            f"Synced to SaathiMart — product '{product_ref}' is now sellable "
            f"at NPR {price:,.2f} with {qty:g} in stock."
        ),
    }

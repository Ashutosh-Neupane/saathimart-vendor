import frappe
from saathimart_vendor.utils import get_config, enqueue_outbox, generate_event_id, next_event_seq


def on_item_price_change(doc, method):
    """
    ERPNext Item Price row changed → if that item is mapped to a hub
    product, push price.update to the Sync Outbox so the hub's Vendor
    Listing for this vendor stays in sync.

    Consumer: saathimart.api.events._apply_price_update (hub side). Only
    selling prices matter here — a buying-side Item Price (purchase cost)
    has nothing to do with what the vendor charges the hub's customers.
    """
    if not doc.selling:
        return

    config = get_config()
    if not config:
        return

    mapping = frappe.db.get_value(
        "Product Mapping",
        {"item_code": doc.item_code, "vendor": config.vendor_id, "sync_status": "Mapped"},
        ["hub_product_id", "barcode"],
        as_dict=True,
    )
    if not mapping or not mapping.hub_product_id:
        return

    enqueue_outbox(
        event_type="price.update",
        payload={
            "product_id": mapping.hub_product_id,
            "vendor": config.vendor_id,
            "price": doc.price_list_rate,
            "sku": doc.item_code,
            "barcode": mapping.barcode or "",
            "event_id": generate_event_id(),
            "event_seq": next_event_seq(),
        },
        voucher_type="Item Price", voucher_no=doc.name,
    )

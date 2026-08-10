import frappe
from saathimart_vendor.utils import get_config, enqueue_outbox, get_site_url, next_event_seq, generate_event_id


def on_sales_order_submit(doc, method):
    hub_order_id = frappe.db.get_value(
        "Vendor Order", {"sales_order": doc.name}, "hub_order_id"
    )
    if not hub_order_id:
        return
    config = get_config()
    if not config:
        return
    enqueue_outbox(
        event_type="order.confirmed",
        payload={
            "order_id": hub_order_id,
            "vendor_id": config.vendor_id,
            "sales_order": doc.name,
            "event_id": generate_event_id(),
            "event_seq": next_event_seq(),
        },
        voucher_type="Sales Order", voucher_no=doc.name,
    )


def on_sales_order_cancel(doc, method):
    hub_order_id = frappe.db.get_value(
        "Vendor Order", {"sales_order": doc.name}, "hub_order_id"
    )
    if not hub_order_id:
        return
    config = get_config()
    if not config:
        return
    enqueue_outbox(
        event_type="order.cancel",
        payload={
            "order_id": hub_order_id,
            "vendor_id": config.vendor_id,
            "reason": "Sales Order cancelled on vendor site",
            "event_id": generate_event_id(),
            "event_seq": next_event_seq(),
        },
        voucher_type="Sales Order Cancel", voucher_no=doc.name,
    )
    vendor_order_name = frappe.db.get_value("Vendor Order", {"sales_order": doc.name}, "name")
    if vendor_order_name:
        frappe.db.set_value("Vendor Order", vendor_order_name, "status", "Cancelled")


def on_delivery_note_submit(doc, method):
    so_name = None
    for item in doc.items:
        if getattr(item, "against_sales_order", None):
            so_name = item.against_sales_order
            break
    if not so_name:
        return
    hub_order_id = frappe.db.get_value(
        "Vendor Order", {"sales_order": so_name}, "hub_order_id"
    )
    if not hub_order_id:
        return
    config = get_config()
    if not config:
        return
    enqueue_outbox(
        event_type="order.dispatched",
        payload={
            "order_id": hub_order_id,
            "vendor_id": config.vendor_id,
            "delivery_note": doc.name,
            "event_id": generate_event_id(),
            "event_seq": next_event_seq(),
        },
        voucher_type="Delivery Note", voucher_no=doc.name,
    )
    frappe.db.set_value("Vendor Order", {"sales_order": so_name}, "status", "Dispatched")
    frappe.db.set_value("Vendor Order", {"sales_order": so_name},
                        "dispatched_at", frappe.utils.now_datetime())


def on_delivery_note_cancel(doc, method):
    so_name = None
    for item in doc.items:
        if getattr(item, "against_sales_order", None):
            so_name = item.against_sales_order
            break
    if not so_name:
        return
    frappe.db.set_value("Vendor Order", {"sales_order": so_name}, "status", "Accepted")

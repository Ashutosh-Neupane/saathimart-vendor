import frappe
from saathimart_vendor.utils import get_config, enqueue_outbox, get_site_url, next_event_seq, generate_event_id


def on_sales_order_submit(doc, method):
    if frappe.flags.get("in_vendor_order_accept"):
        # VendorOrder.accept_order() is submitting this Sales Order itself
        # and will enqueue order.confirmed on its own — skip to avoid a
        # duplicate event.
        return
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
    if frappe.flags.get("in_vendor_order_cancel"):
        # VendorOrder.cancel_order() is cancelling this Sales Order itself
        # and will set the status + enqueue order.cancel on its own (with
        # the vendor-supplied reason) — skip to avoid a duplicate event.
        return
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
    vendor_order = frappe.db.get_value(
        "Vendor Order", {"sales_order": so_name}, ["name", "hub_order_id", "status"], as_dict=True
    )
    if not vendor_order or not vendor_order.hub_order_id:
        return
    if vendor_order.status == "Dispatched":
        # VendorOrder.mark_dispatched() already advanced this order (e.g. a
        # vendor who fulfills instantly clicked the button before a real
        # Delivery Note was ever created for it) — a Delivery Note submitted
        # afterwards for bookkeeping shouldn't re-emit order.dispatched.
        return
    config = get_config()
    if not config:
        return
    enqueue_outbox(
        event_type="order.dispatched",
        payload={
            "order_id": vendor_order.hub_order_id,
            "vendor_id": config.vendor_id,
            "delivery_note": doc.name,
            "event_id": generate_event_id(),
            "event_seq": next_event_seq(),
        },
        voucher_type="Delivery Note", voucher_no=doc.name,
    )
    frappe.db.set_value("Vendor Order", vendor_order.name, {
        "status": "Dispatched",
        "dispatched_at": frappe.utils.now_datetime(),
    })


def on_delivery_note_cancel(doc, method):
    so_name = None
    for item in doc.items:
        if getattr(item, "against_sales_order", None):
            so_name = item.against_sales_order
            break
    if not so_name:
        return
    vendor_order = frappe.db.get_value(
        "Vendor Order", {"sales_order": so_name}, ["name", "status"], as_dict=True
    )
    if not vendor_order or vendor_order.status != "Dispatched":
        # Only revert a status this same Delivery Note actually caused.
        # If the vendor has since marked it Delivered (or it was already
        # Cancelled), a Delivery Note cancellation for paperwork reasons
        # shouldn't regress that.
        return
    frappe.db.set_value("Vendor Order", vendor_order.name, "status", "Accepted")

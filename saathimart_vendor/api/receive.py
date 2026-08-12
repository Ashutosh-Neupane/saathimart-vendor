import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.utils import flt, now_datetime
from saathimart_vendor.utils import get_config, VALID_TRANSITIONS, hub_get, hub_post


def _validate_transition(old_status, new_status):
    """Validate that a status transition is allowed."""
    if old_status == new_status:
        return True
    allowed = VALID_TRANSITIONS.get(old_status, [])
    if new_status not in allowed:
        frappe.throw(_(
            "Invalid status transition: {0} → {1}. Allowed: {2}"
        ).format(old_status, new_status, ", ".join(allowed) if allowed else "none (terminal)"))
    return True


def dispatch_event(event, payload):
    """
    Shared handler dispatch — used both by the live webhook
    (receive_from_hub, below) and by catch-up polling
    (saathimart_vendor.tasks.catch_up_with_hub). Keeping this in one place
    means an event replayed via catch-up runs through exactly the same
    idempotent handler as one delivered live; there's no separate
    "replay" code path that could drift out of sync with the real one.
    """
    handlers = {
        "order.new":      _handle_new_order,
        "order.cancel":   _handle_order_cancel,
        "order.reassign": _handle_order_reassign,
        "product.new":    _handle_new_product,
    }
    handler = handlers.get(event)
    if handler:
        handler(payload)
    else:
        frappe.log_error(f"Unknown event from hub: {event}", "Vendor Receive")


@frappe.whitelist(allow_guest=True)
def receive_from_hub(event=None, payload=None):
    """Hub pushes events here. Idempotent on all handlers."""
    _verify_hub_secret()
    _verify_timestamp()
    if not event:
        frappe.throw(_("event is required"))

    if isinstance(payload, str):
        payload = json.loads(payload)
    payload = payload or {}

    dispatch_event(event, payload)

    frappe.db.commit()
    return {"ok": True}


def _handle_new_order(payload):
    hub_order_id = payload.get("order_id")
    if not hub_order_id:
        frappe.log_error("order.new missing order_id", "Vendor Receive")
        return

    if frappe.db.exists("Vendor Order", hub_order_id):
        return

    config = get_config()
    unmapped_notes = []

    doc = frappe.new_doc("Vendor Order")
    doc.hub_order_id     = hub_order_id
    doc.status           = "Received"
    doc.customer_name    = payload.get("customer_name", "")
    doc.customer_phone   = payload.get("customer_phone", "")
    doc.delivery_address = payload.get("delivery_address", "")
    doc.delivery_lat     = payload.get("delivery_lat") or 0
    doc.delivery_lng     = payload.get("delivery_lng") or 0
    doc.grand_total      = payload.get("grand_total") or 0
    doc.payment_method   = payload.get("payment_method", "")
    doc.payment_status   = payload.get("payment_status", "Unpaid")
    doc.received_at      = frappe.utils.now_datetime()

    for item in (payload.get("items") or []):
        product_id  = item.get("product", "")
        qty         = item.get("qty", 1)
        rate        = item.get("rate", 0)
        item_code   = ""

        if product_id and config:
            mapping = frappe.db.get_value(
                "Product Mapping",
                {"hub_product_id": product_id, "vendor": config.vendor_id, "sync_status": "Mapped"},
                ["item_code", "barcode"],
                as_dict=True,
            )
            if mapping:
                item_code = mapping.item_code or ""
            else:
                hub_result = hub_get(config, "saathimart.api.products.lookup_by_barcode",
                                     {"barcode": product_id})
                if hub_result and hub_result.get("sku"):
                    existing = frappe.db.get_value(
                        "Product Mapping",
                        {"barcode": hub_result["sku"], "vendor": config.vendor_id},
                        ["name", "item_code"],
                        as_dict=True,
                    )
                    if existing:
                        frappe.db.set_value("Product Mapping", existing.name, {
                            "hub_product_id": product_id,
                            "sync_status":    "Mapped",
                            "sync_error":     "",
                            "last_synced":    frappe.utils.now_datetime(),
                        })
                        item_code = existing.item_code or ""
                    else:
                        pm = frappe.new_doc("Product Mapping")
                        pm.barcode         = hub_result["sku"]
                        pm.item_code       = ""
                        pm.vendor          = config.vendor_id
                        pm.hub_product_id  = product_id
                        pm.hub_sku         = hub_result.get("sku", "")
                        pm.sync_status     = "Unmapped"
                        pm.insert(ignore_permissions=True)
                        unmapped_notes.append(
                            f"Auto-created mapping for {product_id} (barcode: {hub_result['sku']}) — item not assigned"
                        )
                else:
                    unmapped_notes.append(f"Product {product_id} not found on hub for auto-mapping")

        if item_code and qty > 0 and config and config.default_warehouse:
            actual_qty = frappe.db.get_value(
                "Bin", {"item_code": item_code, "warehouse": config.default_warehouse}, "actual_qty"
            ) or 0
            if flt(actual_qty) < flt(qty):
                reason = f"Insufficient stock for {item_code}: has {actual_qty}, needs {qty}"
                hub_post(config, "saathimart.api.events.receive", {
                    "event": "order.cancel",
                    "payload": {
                        "order_id": hub_order_id,
                        "vendor_id": config.vendor_id,
                        "reason": reason,
                    },
                })
                frappe.log_error(f"Rejected order {hub_order_id}: {reason}", "Vendor Stock Shortage")
                return

        doc.append("items", {
            "product":   product_id,
            "item_code": item_code,
            "qty":       qty,
            "rate":      rate,
        })

    doc.insert(ignore_permissions=True)

    if unmapped_notes:
        doc.notes = "\n".join(unmapped_notes)
        doc.save(ignore_permissions=True)

    frappe.publish_realtime(
        "new_saathimart_order",
        {"order_id": hub_order_id, "total": payload.get("grand_total")},
    )


def _handle_order_cancel(payload):
    hub_order_id = payload.get("order_id")
    if not hub_order_id or not frappe.db.exists("Vendor Order", hub_order_id):
        return

    doc = frappe.get_doc("Vendor Order", hub_order_id)
    if doc.status in ("Delivered", "Cancelled"):
        return
    _validate_transition(doc.status, "Cancelled")

    if doc.sales_order:
        so = frappe.get_doc("Sales Order", doc.sales_order)
        if so.docstatus == 1:
            try:
                so.cancel()
            except Exception as e:
                frappe.log_error(str(e), f"Cancel SO for hub order {hub_order_id}")

    doc.status = "Cancelled"
    doc.notes = f"Cancelled by hub: {payload.get('reason', '')}"
    doc.save(ignore_permissions=True)


def _handle_order_reassign(payload):
    hub_order_id = payload.get("order_id")
    if not hub_order_id or not frappe.db.exists("Vendor Order", hub_order_id):
        return

    doc = frappe.get_doc("Vendor Order", hub_order_id)
    _validate_transition(doc.status, "Cancelled")

    frappe.db.set_value("Vendor Order", hub_order_id, {
        "status": "Cancelled",
        "notes":  f"Reassigned by hub: {payload.get('reason', '')}",
    })


def _handle_new_product(payload):
    """
    Hub announces a newly-created Product. Auto-map it only when this vendor
    already stocks the exact same physical item — i.e. the barcode matches
    an existing ERPNext Item Barcode row on this site. This is not a "here's
    something you could carry" suggestion queue: if there's no match, this
    is a no-op, on purpose — carrying a product is still entirely the
    vendor's own decision, made through their own inventory, not something
    the hub pushes onto them.

    On a match: creates a fully-Mapped Product Mapping (no manual step) and
    triggers the same auto-create-Vendor-Listing call sync_with_hub() uses,
    so the vendor's existing barcode data alone is enough to make the
    product connected end-to-end — price and stock are still whatever the
    vendor's Item Price / stock ledger already say (or will say once
    entered), same as any other mapping.
    """
    hub_product_id = payload.get("product_id")
    barcode = payload.get("barcode")
    if not hub_product_id or not barcode:
        return

    config = get_config()
    if not config:
        return

    if frappe.db.exists("Product Mapping", {"hub_product_id": hub_product_id, "vendor": config.vendor_id}):
        return  # already known — vendor mapped it some other way already

    item_code = frappe.db.get_value("Item Barcode", {"barcode": barcode}, "parent")
    if not item_code:
        return  # vendor doesn't stock this barcode — nothing to do

    mapping = frappe.new_doc("Product Mapping")
    mapping.barcode = barcode
    mapping.item_code = item_code
    mapping.vendor = config.vendor_id
    mapping.hub_product_id = hub_product_id
    mapping.hub_sku = barcode
    mapping.sync_status = "Mapped"
    mapping.last_synced = now_datetime()
    try:
        mapping.insert(ignore_permissions=True)
    except frappe.ValidationError as e:
        # Most likely this item_code is already mapped to a *different* hub
        # product for this vendor — a real conflict for a human to resolve,
        # not something retrying this same push will ever fix on its own.
        frappe.log_error(
            f"Auto-map skipped: {item_code} / {barcode} for vendor {config.vendor_id}: {e}",
            "Product Auto-Map Conflict",
        )
        return

    mapping._auto_create_vendor_listing(None)


def _verify_hub_secret():
    config = get_config()
    if not config:
        frappe.throw(_("Vendor Config not set up"), frappe.AuthenticationError)
    secret = ""
    try:
        secret = config.get_password("webhook_secret", raise_exception=False) or ""
    except Exception:
        pass
    if not secret:
        frappe.throw(_("Webhook secret not configured"), frappe.AuthenticationError)
    incoming = frappe.request.headers.get("X-SM-Secret", "")
    if not hmac.compare_digest(incoming, secret):
        _log_auth_failure("receive_from_hub", "invalid_secret")
        frappe.throw(_("Invalid webhook secret"), frappe.AuthenticationError)


def _verify_timestamp(max_age_seconds=300):
    """Reject events older than max_age_seconds to prevent replay attacks."""
    from datetime import datetime, timezone
    ts = frappe.request.headers.get("X-SM-Timestamp")
    if not ts:
        frappe.throw(_("Missing X-SM-Timestamp header"), frappe.AuthenticationError)
    try:
        event_time = float(ts)
    except (TypeError, ValueError):
        frappe.throw(_("Invalid timestamp"), frappe.AuthenticationError)
    now = datetime.now(timezone.utc).timestamp()
    if abs(now - event_time) > max_age_seconds:
        frappe.throw(_("Event timestamp too old"), frappe.AuthenticationError)


def _log_auth_failure(endpoint, reason, payload=None):
    """Log failed authentication attempts for security monitoring."""
    try:
        ip = frappe.get_request_header("X-Forwarded-For", "").split(",")[0].strip() or \
             frappe.get_request_header("X-Real-IP", "") or \
             (frappe.request.ip if frappe.request else "unknown")
        user_agent = frappe.get_request_header("User-Agent", "")
        payload_hash = ""
        if payload:
            payload_hash = hashlib.sha256(
                json.dumps(payload, default=str).encode()
            ).hexdigest()[:16]
        frappe.log_error(
            f"Auth failure: endpoint={endpoint} reason={reason} ip={ip} "
            f"user_agent={user_agent} payload_hash={payload_hash}",
            "Webhook Auth Failure",
        )
    except Exception:
        pass

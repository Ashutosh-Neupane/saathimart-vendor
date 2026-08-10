import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.utils import now_datetime
from saathimart_vendor.utils import get_config, VALID_TRANSITIONS


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

    handlers = {
        "order.new":      _handle_new_order,
        "order.cancel":   _handle_order_cancel,
        "order.reassign": _handle_order_reassign,
    }
    handler = handlers.get(event)
    if handler:
        handler(payload)
    else:
        frappe.log_error(f"Unknown event from hub: {event}", "Vendor Receive")

    frappe.db.commit()
    return {"ok": True}


def _handle_new_order(payload):
    hub_order_id = payload.get("order_id")
    if not hub_order_id:
        frappe.log_error("order.new missing order_id", "Vendor Receive")
        return

    if frappe.db.exists("Vendor Order", hub_order_id):
        return

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
        doc.append("items", {
            "product":  item.get("product", ""),
            "item_code": item.get("item_code", ""),
            "qty":      item.get("qty", 1),
            "rate":     item.get("rate", 0),
        })

    doc.insert(ignore_permissions=True)

    frappe.publish_realtime(
        "new_saathimart_order",
        {"order_id": hub_order_id, "total": payload.get("grand_total")},
    )


def _handle_order_cancel(payload):
    hub_order_id = payload.get("order_id")
    if not hub_order_id or not frappe.db.exists("Vendor Order", hub_order_id):
        return

    doc = frappe.get_doc("Vendor Order", hub_order_id)
    _validate_transition(doc.status, "Cancelled")

    if doc.status in ("Delivered", "Cancelled"):
        return

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
    ts = frappe.request.headers.get("X-SM-Timestamp")
    if not ts:
        frappe.throw(_("Missing X-SM-Timestamp header"), frappe.AuthenticationError)
    try:
        event_time = float(ts)
    except (TypeError, ValueError):
        frappe.throw(_("Invalid timestamp"), frappe.AuthenticationError)
    now = now_datetime().timestamp()
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

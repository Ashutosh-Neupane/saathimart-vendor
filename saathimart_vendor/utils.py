import hashlib
import hmac
import json
import uuid
import frappe
import requests
from datetime import datetime, timezone
from frappe.utils import flt


def safe_enqueue(*args, **kwargs):
    """
    frappe.enqueue(), but never lets a background-job scheduling failure
    break the caller. Found live (bench run-tests, hub side): frappe.enqueue()
    itself can raise QueueOverloaded (Frappe's own cap on pending RQ jobs)
    when nothing is draining the queue fast enough — and every call site
    here runs synchronously inside a whitelisted vendor action or ERPNext
    doc_event hook (accept_order, mark_dispatched, an Item save, ...). An
    uncaught QueueOverloaded there doesn't just skip the instant-delivery
    optimization, it fails the underlying action itself — e.g. a vendor
    unable to accept an order because the "sync this to the hub faster"
    nicety couldn't be scheduled. flush_outbox's cron sweep is the fallback
    exactly so instant delivery can be allowed to fail silently.
    """
    try:
        frappe.enqueue(*args, **kwargs)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Instant delivery scheduling failed")


def get_config():
    """Return Vendor Config single doc or None if not set up."""
    try:
        c = frappe.get_single("Vendor Config")
        return c if c.hub_url and c.vendor_id else None
    except Exception:
        return None


def get_vendor_id():
    c = get_config()
    return c.vendor_id if c else ""


def get_site_url():
    try:
        from frappe.utils import get_url
        return get_url()
    except Exception:
        return frappe.local.site or ""


def get_mapping(item_code):
    """Return Product Mapping for an ERPNext item_code, or None."""
    name = frappe.db.get_value(
        "Product Mapping",
        {"item_code": item_code, "is_active": 1, "sync_status": "Mapped"},
        "name",
    )
    return frappe.get_doc("Product Mapping", name) if name else None


def get_mapping_by_barcode(barcode):
    name = frappe.db.get_value(
        "Product Mapping",
        {"barcode": barcode, "is_active": 1},
        "name",
    )
    return frappe.get_doc("Product Mapping", name) if name else None


def log_unmapped(item_code, voucher_no):
    frappe.log_error(
        f"No Product Mapping for item_code={item_code} voucher={voucher_no}. "
        "Create a mapping in Product Mapping.",
        "SaathiMart Unmapped Item",
    )


def generate_event_id():
    return str(uuid.uuid4())


VALID_TRANSITIONS = {
    "Received":    ["Accepted", "Cancelled"],
    # "Preparing" is an optional intermediate status — mark_dispatched()'s
    # own guard (`status not in ("Accepted", "Preparing")`) already allows
    # dispatching straight from "Accepted" for vendors who fulfill
    # instantly, so this map must allow it too or that guard is dead code.
    "Accepted":    ["Preparing", "Dispatched", "Cancelled"],
    "Preparing":   ["Dispatched", "Cancelled"],
    "Dispatched":  ["Delivered"],
    "Delivered":   [],
    "Cancelled":   [],
}


def next_event_seq():
    """Return the next monotonically increasing event sequence for this vendor."""
    config = get_config()
    if not config:
        return 1
    last = config.last_event_seq or 0
    seq = last + 1
    frappe.db.set_value("Vendor Config", config.name, "last_event_seq", seq)
    return seq


def build_stock_payload(mapping, qty_change, voucher_type, voucher_no, source_site, remarks, base_qty=None, warehouse=None):
    """
    Build a stock event payload with idempotency and ordering fields.
    base_qty is the expected total qty (available + reserved) at push time,
    used by the hub as a staleness guard.
    warehouse is the ERPNext warehouse name — included so the hub can track
    stock per location for multi-warehouse vendors.
    """
    payload = {
        "barcode": mapping.barcode,
        "hub_product": mapping.hub_product_id,
        "qty_change": flt(qty_change),
        "vendor_id": get_vendor_id(),
        "voucher_no": voucher_no or "",
        "source_site": source_site or "",
        "remarks": remarks or "",
        "event_id": generate_event_id(),
        "event_seq": next_event_seq(),
    }
    if base_qty is not None:
        payload["base_qty"] = flt(base_qty)
    if warehouse:
        payload["warehouse"] = warehouse
    return payload


def _get_base_qty(item_code, warehouse=None):
    """Return available + reserved qty from ERPNext Bin for staleness guard.
    If warehouse is specified, queries that specific warehouse; otherwise uses
    the default warehouse from Vendor Config.
    """
    config = get_config()
    wh = warehouse or (config.default_warehouse if config else None)
    if not wh:
        return None
    actual = frappe.db.get_value(
        "Bin", {"item_code": item_code, "warehouse": wh}, "actual_qty"
    ) or 0
    reserved = frappe.db.get_value(
        "Bin", {"item_code": item_code, "warehouse": wh}, "reserved_qty"
    ) or 0
    return flt(actual) + flt(reserved)


def create_payment_entry_for_order(vendor_order, reference_no="", paid_amount=None):
    """
    Create + submit a real ERPNext Payment Entry against the vendor order's
    submitted Sales Order, so money the hub actually collected (eSewa) shows
    up in the vendor's own books — Accounts Receivable cleared, bank/cash
    debited — instead of the sale staying perpetually uncollected on paper.

    Idempotent: a second call for an order that already has a Payment Entry
    is a no-op. Failures are logged, never raised — a missing/misconfigured
    chart of accounts on the vendor site must not block order acceptance or
    event processing; the entry can be created by hand later.

    Returns the Payment Entry name, or None when nothing was created.
    """
    if not vendor_order.sales_order:
        return None
    if getattr(vendor_order, "payment_entry", None):
        return vendor_order.payment_entry

    # get_payment_entry() builds allocations against a *submitted* document;
    # a draft SO has nothing outstanding to allocate yet.
    so_docstatus = frappe.db.get_value("Sales Order", vendor_order.sales_order, "docstatus")
    if so_docstatus != 1:
        return None

    try:
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        pe = get_payment_entry("Sales Order", vendor_order.sales_order)
        pe.reference_no = reference_no or f"HUB-{vendor_order.hub_order_id}"
        pe.reference_date = frappe.utils.today()

        # The hub sends this vendor's slice of what was collected. Trust it
        # only when it's present and sane — otherwise keep the SO-outstanding
        # amounts get_payment_entry already computed.
        amount = flt(paid_amount) if paid_amount is not None else 0
        if amount > 0:
            pe.paid_amount = amount
            pe.received_amount = amount
            for ref in (pe.references or []):
                ref.allocated_amount = amount

        pe.insert(ignore_permissions=True)
        pe.submit()

        frappe.db.set_value("Vendor Order", vendor_order.name, {
            "payment_status": "Paid",
            "paid_at": frappe.utils.now_datetime(),
            "payment_entry": pe.name,
        })
        return pe.name
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Payment Entry failed for hub order {vendor_order.hub_order_id}",
        )
        return None


def enqueue_outbox(event_type, payload, voucher_type="", voucher_no=""):
    """
    Write one row to Sync Outbox in the SAME db transaction as the caller.
    Transactional outbox pattern — event is never lost even if hub is down.

    Schedules immediate delivery of just this row (enqueue_after_commit=True
    defers the actual RQ job until this transaction commits). This used to
    have an immediate=True/False split with a "batch, don't push yet" path
    for bulk-ish event types — removed: the batching was an in-memory
    module-level buffer, which can't work correctly here. Every
    enqueue_outbox() call can run in a different process (any gunicorn
    worker handling whatever request/hook triggered it), and a background
    job scheduled to "flush the buffer later" runs in a *different* process
    again (an RQ worker) — Python module globals aren't shared across OS
    processes, so that deferred flush always found an empty buffer and
    silently did nothing. Real batching for genuine bulk scenarios (many
    pending rows at once) now happens in flush_outbox()'s cron sweep
    instead, which reads the actually-durable Sync Outbox table rather than
    in-memory state — see its docstring.
    """
    doc = frappe.new_doc("Sync Outbox")
    doc.event_type = event_type
    doc.payload = json.dumps(payload, default=str)
    doc.status = "Pending"
    doc.voucher_type = voucher_type
    doc.voucher_no = voucher_no
    doc.insert(ignore_permissions=True)
    # intentionally no frappe.db.commit() here — caller's transaction covers this

    safe_enqueue(
        "saathimart_vendor.tasks._push_one_now",
        row_name=doc.name,
        queue="default",
        enqueue_after_commit=True,
        job_id=f"push-sync-outbox-{doc.name}",
        deduplicate=True,
    )


def hub_get(config, method, params=None):
    """GET request to hub API. Returns message dict or None."""
    try:
        resp = requests.get(
            f"{config.hub_url}/api/method/{method}",
            params=params or {},
            headers=hub_headers(config),
            timeout=10,
        )
        if resp.ok:
            return resp.json().get("message")
        frappe.log_error(f"hub_get {method} → {resp.status_code}: {resp.text[:300]}",
                         "SaathiMart Hub GET")
        return None
    except Exception as e:
        frappe.log_error(str(e), "SaathiMart Hub GET")
        return None


def hub_post(config, method, payload):
    """POST request to hub API. Returns (ok, message_or_error)."""
    try:
        body = json.dumps(payload)
        resp = requests.post(
            f"{config.hub_url}/api/method/{method}",
            data=body,
            headers=hub_headers(config, body=body),
            timeout=10,
        )
        if resp.ok:
            return True, resp.json().get("message")
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return False, str(e)[:300]


def compute_hmac_signature(secret, timestamp, body):
    """
    Stripe-style request signature: HMAC-SHA256 over "<timestamp>.<body>"
    keyed with the shared webhook secret. Mirrors the hub's helper so both
    sides sign and verify identically. The secret never crosses the wire.
    """
    msg = f"{timestamp}.".encode() + (body if isinstance(body, bytes) else body.encode())
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def hub_headers(config, body=""):
    secret = ""
    try:
        secret = config.get_password("webhook_secret", raise_exception=False) or ""
    except Exception:
        pass
    ts = str(int(datetime.now(timezone.utc).timestamp()))
    headers = {
        "X-Vendor-ID": config.vendor_id,
        "X-SM-Timestamp": ts,
        "Content-Type": "application/json",
    }
    # HMAC-SHA256 signature over "<timestamp>.<body>" — the secret never
    # crosses the wire. Legacy bare X-SM-Secret header removed.
    if secret:
        headers["X-SM-Signature"] = compute_hmac_signature(secret, ts, body)
    # hub_url is often a Docker service name (e.g. http://hub:8000), which
    # is not itself a valid Frappe site — an explicit Host header is what
    # routes the request to the right site on a multi-tenant hub bench.
    if config.hub_site:
        headers["Host"] = config.hub_site
    return headers

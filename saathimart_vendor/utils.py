import json
import uuid
import frappe
import requests
from frappe.utils import flt


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
    "Accepted":    ["Preparing", "Cancelled"],
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


def build_stock_payload(mapping, qty_change, voucher_type, voucher_no, source_site, remarks, base_qty=None):
    """
    Build a stock event payload with idempotency and ordering fields.
    base_qty is the expected total qty (available + reserved) at push time,
    used by the hub as a staleness guard.
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
    return payload


def _get_base_qty(item_code):
    """Return available + reserved qty from ERPNext Bin for staleness guard."""
    config = get_config()
    if not config or not config.default_warehouse:
        return None
    actual = frappe.db.get_value(
        "Bin", {"item_code": item_code, "warehouse": config.default_warehouse}, "actual_qty"
    ) or 0
    reserved = frappe.db.get_value(
        "Bin", {"item_code": item_code, "warehouse": config.default_warehouse}, "reserved_qty"
    ) or 0
    return flt(actual) + flt(reserved)


def enqueue_outbox(event_type, payload, voucher_type="", voucher_no=""):
    """
    Write one row to Sync Outbox in the SAME db transaction as the caller.
    Transactional outbox pattern — event is never lost even if hub is down.
    """
    doc = frappe.new_doc("Sync Outbox")
    doc.event_type = event_type
    doc.payload = json.dumps(payload, default=str)
    doc.status = "Pending"
    doc.voucher_type = voucher_type
    doc.voucher_no = voucher_no
    doc.insert(ignore_permissions=True)
    # intentionally no frappe.db.commit() here — caller's transaction covers this


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
        resp = requests.post(
            f"{config.hub_url}/api/method/{method}",
            json=payload,
            headers=hub_headers(config),
            timeout=10,
        )
        if resp.ok:
            return True, resp.json().get("message")
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return False, str(e)[:300]


def hub_headers(config):
    secret = ""
    try:
        secret = config.get_password("webhook_secret", raise_exception=False) or ""
    except Exception:
        pass
    headers = {
        "X-SM-Secret": secret,
        "X-Vendor-ID": config.vendor_id,
        "Content-Type": "application/json",
    }
    # hub_url is often a Docker service name (e.g. http://hub:8000), which
    # is not itself a valid Frappe site — an explicit Host header is what
    # routes the request to the right site on a multi-tenant hub bench.
    if config.hub_site:
        headers["Host"] = config.hub_site
    return headers

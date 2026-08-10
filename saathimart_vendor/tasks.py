import json
import frappe
import requests
from frappe.utils import now_datetime, add_to_date, nowdate, get_url
from saathimart_vendor.utils import get_config, enqueue_outbox, hub_headers


# ── Outbox flush (every 1 min) ────────────────────────────────────────────────

def flush_outbox():
    config = get_config()
    if not config or not config.sync_enabled:
        return

    pending = frappe.db.sql("""
        SELECT name, event_type, payload, retry_count
        FROM `tabSync Outbox`
        WHERE status = 'Pending'
          AND (next_retry_at IS NULL OR next_retry_at <= NOW())
        ORDER BY creation ASC
        LIMIT 200
    """, as_dict=True)

    for row in pending:
        _push_to_hub(config, row)


def _push_to_hub(config, row):
    try:
        resp = requests.post(
            f"{config.hub_url}/api/method/saathimart.api.events.receive",
            json={
                "event":   row.event_type,
                "payload": json.loads(row.payload or "{}"),
            },
            headers=hub_headers(config),
            timeout=10,
        )
        if resp.ok:
            frappe.db.set_value("Sync Outbox", row.name, {
                "status": "Sent",
                "last_error": "",
            })
        else:
            _handle_failure(config, row, f"HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        _handle_failure(config, row, str(e)[:200])

    frappe.db.commit()


def _handle_failure(config, row, error):
    retry_count = (row.retry_count or 0) + 1
    backoff_minutes = min(2 ** retry_count, 60)
    next_retry = add_to_date(now_datetime(), minutes=backoff_minutes)
    status = "Dead" if retry_count > 10 else "Pending"

    frappe.db.set_value("Sync Outbox", row.name, {
        "status":        status,
        "retry_count":   retry_count,
        "next_retry_at": next_retry,
        "last_error":    error,
    })

    if status == "Dead":
        admin_email = frappe.db.get_single_value("Vendor Config", "admin_email") or ""
        if admin_email:
            frappe.sendmail(
                recipients=[admin_email],
                subject=f"[SaathiMart] Sync failed: {row.event_type}",
                message=(
                    f"Outbox entry {row.name} ({row.event_type}) failed after "
                    f"10 retries and is now Dead.\n\nLast error: {error}\n\n"
                    f"Please check Sync Outbox in your ERPNext desk and re-queue manually."
                ),
            )
        frappe.log_error(
            f"Outbox {row.name} dead after 10 retries. Error: {error}",
            "SaathiMart Outbox Dead",
        )


# ── Hub health check (every 5 min) ───────────────────────────────────────────

def check_hub_health():
    config = get_config()
    if not config:
        return

    try:
        resp = requests.get(f"{config.hub_url}/api/method/ping", headers=hub_headers(config), timeout=5)
        status = "Active" if resp.ok else "Unreachable"
    except Exception:
        status = "Unreachable"

    frappe.db.set_value("Vendor Config", config.name, "hub_status", status)

    if status == "Unreachable":
        pending_count = frappe.db.count("Sync Outbox", {"status": "Pending"})
        frappe.log_error(
            f"SaathiMart hub unreachable at {config.hub_url}. "
            f"{pending_count} events pending in outbox.",
            "SaathiMart Hub Unreachable",
        )

    frappe.db.commit()


# ── Hourly stock reconciliation ───────────────────────────────────────────────

def reconcile_stock():
    config = get_config()
    if not config or not config.reconciliation_enabled:
        return

    mappings = frappe.get_list(
        "Product Mapping",
        filters={"is_active": 1, "sync_status": "Mapped"},
        fields=["name", "barcode", "item_code", "hub_product_id"],
    )

    if not mappings:
        frappe.db.set_value("Vendor Config", config.name, "last_sync_at", now_datetime())
        frappe.db.commit()
        return

    chunk_size = 200
    chunks = [mappings[i:i + chunk_size] for i in range(0, len(mappings), chunk_size)]

    for idx, chunk in enumerate(chunks):
        frappe.enqueue(
            "saathimart_vendor.tasks._reconcile_chunk",
            config_name=config.name,
            mappings=chunk,
            queue="short",
            job_id=f"reconcile_chunk_{config.name}_{idx}_{now_datetime().strftime('%Y%m%d%H%M%S%f')}",
        )

    frappe.db.set_value("Vendor Config", config.name, "last_sync_at", now_datetime())
    frappe.db.commit()


def _reconcile_chunk(config_name, mappings):
    """Reconcile a chunk of product mappings in a background worker."""
    config = frappe.get_doc("Vendor Config", config_name)
    if not config or not config.reconciliation_enabled:
        return

    products = [m.hub_product_id for m in mappings if m.hub_product_id]
    batch_result = {}
    if products:
        try:
            resp = requests.get(
                f"{config.hub_url}/api/method/saathimart.api.stock.get_vendor_stock_batch",
                params={"vendor": config.vendor_id, "products": ",".join(products)},
                headers=hub_headers(config),
                timeout=30,
            )
            if resp.ok:
                batch_result = resp.json().get("message", {}) or {}
        except Exception:
            pass

    for m in mappings:
        try:
            _reconcile_item(config, m, batch_result)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Reconciliation failed for {m.item_code}",
            )


def _reconcile_item(config, mapping, batch_result=None):
    warehouse = config.default_warehouse
    if not warehouse:
        return

    actual_qty = frappe.db.get_value(
        "Bin",
        {"item_code": mapping.item_code, "warehouse": warehouse},
        "actual_qty",
    ) or 0.0

    hub_product = mapping.hub_product_id
    hub_physical = 0.0
    if batch_result and hub_product and hub_product in batch_result:
        hub_physical = float(batch_result[hub_product].get("physical_qty", 0) or 0)
    elif hub_product:
        try:
            resp = requests.get(
                f"{config.hub_url}/api/method/saathimart.api.stock.get_vendor_stock",
                params={"vendor": config.vendor_id, "product": hub_product},
                headers=hub_headers(config),
                timeout=10,
            )
            if resp.ok:
                hub_physical = float(
                    resp.json().get("message", {}).get("physical_qty", 0) or 0
                )
        except Exception:
            pass

    drift = actual_qty - hub_physical
    threshold = float(config.reconciliation_threshold or 2)
    threshold_pct = float(config.reconciliation_threshold_pct or 5)
    pct_drift = abs(drift / hub_physical * 100) if hub_physical else 100.0

    if abs(drift) > threshold or pct_drift > threshold_pct:
        enqueue_outbox(
            event_type="stock.adjustment",
            payload={
                "barcode":      mapping.barcode,
                "hub_product":  hub_product,
                "qty_change":   drift,
                "voucher_no":   f"RECON-{nowdate()}",
                "vendor_id":    config.vendor_id,
                "source_site":  get_url(),
                "remarks":      (
                    f"Hourly reconciliation. "
                    f"ERPNext={actual_qty} Hub={hub_physical} Drift={drift}"
                ),
            },
            voucher_type="Reconciliation",
            voucher_no=f"RECON-{nowdate()}",
        )

import json
import zlib
import frappe
import requests
from frappe.utils import now_datetime, add_to_date, nowdate, get_url, get_datetime
from saathimart_vendor.utils import get_config, enqueue_outbox, hub_headers, safe_enqueue


# ── Outbox flush (every 1 min) ────────────────────────────────────────────────

BULK_CHUNK_SIZE = 50


def flush_outbox():
    """
    Cron every 1 min — deliver pending Sync Outbox rows. Each row also gets
    its own instant single-row delivery attempt at write time (see
    utils.enqueue_outbox) — this sweep is the fallback for anything that
    missed that (instant push failed, worker pool was backed up) or piled
    up faster than one-at-a-time delivery could drain it.

    Chunks pending rows into groups of BULK_CHUNK_SIZE and sends each
    multi-row group as one bulk_receive HTTP call instead of one row = one
    HTTP call. This is real batching, using the Sync Outbox table itself as
    the buffer — a single-row chunk still goes through the plain
    single-event endpoint, no bulk overhead for the common small case.
    (An earlier version of this batching used an in-memory module-level
    buffer flushed by a separate background job — that couldn't work:
    every enqueue_outbox() call can run in a different gunicorn worker
    process, and the "flush later" job runs in yet another (RQ worker)
    process again, so the deferred flush always found an empty buffer.
    Reading pending rows straight from the DB here doesn't have that
    problem — one process, one query, real data.)
    """
    config = get_config()
    if not config or not config.sync_enabled:
        return

    pending = frappe.db.sql("""
        SELECT name, event_type, payload, retry_count
        FROM `tabSync Outbox`
        WHERE status = 'Pending'
          AND (next_retry_at IS NULL OR next_retry_at <= NOW())
        ORDER BY creation ASC
        LIMIT 1000
    """, as_dict=True)

    for i in range(0, len(pending), BULK_CHUNK_SIZE):
        chunk = pending[i:i + BULK_CHUNK_SIZE]
        if len(chunk) == 1:
            _push_to_hub(config, chunk[0])
        else:
            _push_bulk(config, chunk)


def _push_one_now(row_name):
    """
    Deliver a single pending Sync Outbox row right away, instead of waiting
    for the next flush_outbox cron tick (which stays as the retry/fallback
    sweep for anything the instant path missed). Called via
    saathimart_vendor.utils.enqueue_outbox with enqueue_after_commit=True,
    so this only ever runs once the transaction that wrote the row has
    actually committed.
    """
    config = get_config()
    if not config or not config.sync_enabled:
        return
    row = frappe.db.get_value(
        "Sync Outbox", row_name,
        ["name", "event_type", "payload", "retry_count", "status"],
        as_dict=True,
    )
    if not row or row.status != "Pending":
        return  # already delivered (or picked up) by flush_outbox's cron sweep
    _push_to_hub(config, row)


def _push_bulk(config, rows):
    """
    Deliver multiple pending Sync Outbox rows in a single HTTP POST to the
    hub's bulk_receive endpoint. On failure, every row in the batch gets
    its own retry bookkeeping via _handle_failure — not just the first one
    (a real bug in an earlier version of this: only rows[0] ever got its
    retry_count incremented on a batch failure, so the rest silently never
    escalated to Dead / triggered the admin alert no matter how long they
    kept failing).
    """
    events = [
        {"event": row.event_type, "payload": json.loads(row.payload or "{}")}
        for row in rows
    ]

    try:
        resp = requests.post(
            f"{config.hub_url}/api/method/saathimart.api.events.bulk_receive",
            json={"events": events},
            headers=hub_headers(config),
            timeout=30,
        )
        if resp.ok:
            for row in rows:
                frappe.db.set_value("Sync Outbox", row.name, {
                    "status": "Sent",
                    "last_error": "",
                })
        else:
            error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
            for row in rows:
                _handle_failure(config, row, error_msg)
    except Exception as e:
        error_msg = str(e)[:200]
        for row in rows:
            _handle_failure(config, row, error_msg)

    frappe.db.commit()


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


# ── Catch-up polling (every 5 min) ───────────────────────────────────────────

def catch_up_with_hub():
    """
    Pull any events targeted at this vendor that a plain webhook push might
    have missed entirely — the site was down when the hub tried to push, or
    the hub's own retries were exhausted and the event went Dead. Before
    this, there was no way back for a vendor in that state short of someone
    noticing and manually re-triggering a sync.

    Runs the same handler dispatch as a live push
    (saathimart_vendor.api.receive.dispatch_event), so a replayed event is
    processed identically to one that arrived live — same idempotency
    guarantees, no separate replay logic to keep in sync.

    Cheap when there's nothing to catch up on: a single GET that returns an
    empty list once `since` is caught up to the hub's latest event_seq for
    this vendor.
    """
    config = get_config()
    if not config or not config.sync_enabled:
        return

    from saathimart_vendor.api.receive import dispatch_event

    since = config.last_hub_event_seq or 0
    try:
        resp = requests.get(
            f"{config.hub_url}/api/method/saathimart.api.events.poll",
            params={"since": since, "limit": 50},
            headers=hub_headers(config),
            timeout=15,
        )
    except Exception as e:
        frappe.log_error(str(e), "SaathiMart Catch-Up Poll")
        return

    if not resp.ok:
        return

    events = (resp.json().get("message") or {}).get("events") or []
    max_seq = since
    for evt in events:
        try:
            dispatch_event(evt.get("event_type"), evt.get("payload") or {})
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Catch-up replay failed: {evt.get('event_type')}",
            )
        max_seq = max(max_seq, evt.get("event_seq") or 0)

    if max_seq != since:
        frappe.db.set_value("Vendor Config", config.name, "last_hub_event_seq", max_seq)
        frappe.db.commit()


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


# ── Daily outbox archival ─────────────────────────────────────────────────────

def archive_old_outbox():
    """
    Daily cron — purge old, fully-resolved Sync Outbox rows (Sent or Dead;
    Pending/Failed rows are still live work and left alone). Mirrors the
    hub's own Webhook Event cleanup (saathimart.api.archival.archive_old_data)
    — that job used to also try to delete from this exact table directly on
    the hub's database, which doesn't have it; this is the corrected,
    same-side version.
    """
    frappe.db.sql("""
        DELETE FROM `tabSync Outbox`
        WHERE status IN ('Sent', 'Dead')
          AND creation < DATE_SUB(NOW(), INTERVAL 30 DAY)
        LIMIT 1000
    """)
    frappe.db.commit()


# ── Hourly stock reconciliation ───────────────────────────────────────────────

@frappe.whitelist()
def reconcile_stock(force=False):
    """
    Runs on a */10 cron (see hooks.py) but only actually does work roughly
    once an hour, in a slot derived from this vendor's own vendor_id.

    Why not just a straight "0 * * * *" hourly cron: every vendor site runs
    its own independent bench scheduler, so a fixed top-of-the-hour cron
    means every vendor in the whole system hits the hub's
    get_vendor_stock_batch endpoint in the same handful of seconds, every
    hour, regardless of vendor count — a synchronized thundering herd that
    gets worse the more vendors there are, rather than better. Spreading
    each vendor into a stable ~10-minute slot (hash of vendor_id, so it's
    consistent across runs but different across vendors) turns that
    simultaneous spike into a roughly even trickle across the hour.

    force=True bypasses both the slot and staleness gates — a vendor desk
    user clicking "Reconcile Now" (or a test) shouldn't have to wait for
    their own hourly window.
    """
    config = get_config()
    if not config or not config.reconciliation_enabled:
        return

    if not force:
        vendor_slot = zlib.crc32(config.vendor_id.encode()) % 6
        current_slot = now_datetime().minute // 10
        if vendor_slot != current_slot:
            return

        if config.last_sync_at and (now_datetime() - get_datetime(config.last_sync_at)).total_seconds() < 55 * 60:
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
        safe_enqueue(
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

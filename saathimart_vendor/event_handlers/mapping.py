import frappe
from saathimart_vendor.utils import get_config, enqueue_outbox, generate_event_id, next_event_seq, safe_enqueue


def on_item_barcode_change(doc, method):
    """
    ERPNext Item saved (created or updated). Diffs the barcode child table
    against its pre-save state and hands the actual sync work off to a
    background worker (_sync_item_barcodes) — Item.save() itself should
    never get slower because of how many barcodes are on it, same reasoning
    as saathimart.events.publisher.on_product_created deferring its own
    vendor fan-out to a background job instead of doing it inline.

    get_doc_before_save() is None on a brand-new Item (after_insert), which
    correctly means "everything currently on it is new."
    """
    current = {row.barcode for row in (doc.get("barcodes") or []) if row.barcode}

    previous = set()
    before = doc.get_doc_before_save()
    if before:
        previous = {row.barcode for row in (before.get("barcodes") or []) if row.barcode}

    added = current - previous
    removed = previous - current
    if not added and not removed:
        return

    safe_enqueue(
        "saathimart_vendor.event_handlers.mapping._sync_item_barcodes",
        item_code=doc.name,
        added=list(added),
        removed=list(removed),
        queue="short",
        enqueue_after_commit=True,
        job_id=f"sync-item-barcodes-{doc.name}-{doc.modified}",
    )


def _sync_item_barcodes(item_code, added, removed):
    """
    Background worker: push barcode.register for newly-added barcodes and
    barcode.unregister for removed ones, so the hub's Vendor Barcode Index
    (saathimart.saathimart.doctype.vendor_barcode_index) stays accurate —
    without this, a barcode removed from an Item would stay in the index
    forever, and a future product with that barcode would still notify a
    vendor who no longer actually carries it.

    This is what lets on_product_created (hub side) notify only the
    vendors that actually carry a new product's barcode instead of
    broadcasting to every vendor site — see
    saathimart/events/publisher.py::_broadcast_new_product.
    """
    config = get_config()
    if not config:
        return

    for barcode in added:
        enqueue_outbox(
            event_type="barcode.register",
            payload={
                "vendor": config.vendor_id,
                "barcode": barcode,
                "event_id": generate_event_id(),
                "event_seq": next_event_seq(),
            },
            voucher_type="Item", voucher_no=item_code,
        )

    for barcode in removed:
        enqueue_outbox(
            event_type="barcode.unregister",
            payload={
                "vendor": config.vendor_id,
                "barcode": barcode,
                "event_id": generate_event_id(),
                "event_seq": next_event_seq(),
            },
            voucher_type="Item", voucher_no=item_code,
        )

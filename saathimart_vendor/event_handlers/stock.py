import frappe
from frappe.utils import flt
from saathimart_vendor.utils import get_config, get_mapping, log_unmapped, enqueue_outbox, get_vendor_id, get_site_url, build_stock_payload, _get_base_qty


# Voucher types with a fixed, unambiguous stock direction. Types that can go
# either way (Delivery Note — outward delivery vs. inward customer return;
# Stock Entry — Material Receipt vs. Material Issue) are deliberately left
# out here and resolved from the already-signed qty_change instead — see
# _get_voucher_event_type().
VOUCHER_TYPE_MAP = {
    "Purchase Receipt": "stock.receipt",
    "Purchase Invoice": "stock.receipt",
    "Sales Invoice": "stock.deduct",
    "Stock Reconciliation": "stock.adjustment",
    "Subcontracting Receipt": "stock.receipt",
}

# Stock Entry sub-types that are receipts (stock in)
STOCK_ENTRY_RECEIPT_TYPES = ("Material Receipt", "Manufacture", "Repack", "Subcontract")

# Stock Entry sub-types that are issues (stock out)
STOCK_ENTRY_ISSUE_TYPES = ("Material Issue", "Material Transfer", "Material Transfer for Manufacture", "Material Consumption for Manufacture")


def _get_voucher_event_type(voucher_type, qty_change):
    """
    Determine the stock event type. Fixed-direction voucher types are looked
    up directly; ambiguous ones (Stock Entry, Delivery Note) are derived from
    the sign of the already-resolved qty_change instead.
    """
    if voucher_type in VOUCHER_TYPE_MAP:
        return VOUCHER_TYPE_MAP[voucher_type]
    if qty_change > 0:
        return "stock.receipt"
    if qty_change < 0:
        return "stock.deduct"
    return "stock.adjustment"


def on_stock_ledger_entry_submit(doc, method):
    """
    Called when ANY Stock Ledger Entry is submitted in ERPNext.
    This catches ALL stock movements — Purchase Receipt, Sales Invoice,
    Stock Entry, Delivery Note, Stock Reconciliation, Manufacturing, etc.
    """
    config = get_config()
    if not config or not config.sync_enabled:
        return

    # Skip if this SLE is from a SaathiMart order that was already handled
    # (the order flow handles its own stock events)
    if doc.voucher_type == "Sales Invoice" and _is_saathimart_order_from_si(doc):
        return

    mapping = get_mapping(doc.item_code)
    if not mapping:
        log_unmapped(doc.item_code, doc.voucher_no)
        return

    # Determine qty_change based on whether it's a receipt or issue
    # SLE.actual_qty is the change in stock (positive = in, negative = out)
    qty_change = doc.actual_qty

    # For Stock Entry, we need to determine direction from stock_entry_type
    if doc.voucher_type == "Stock Entry":
        if doc.voucher_detail_no:
            # SLE created from Stock Entry - actual_qty already has the right sign
            pass
        else:
            # Fallback: check stock_entry_type
            try:
                se_doc = frappe.get_doc("Stock Entry", doc.voucher_no)
                if se_doc.stock_entry_type in STOCK_ENTRY_RECEIPT_TYPES:
                    qty_change = abs(doc.actual_qty)
                elif se_doc.stock_entry_type in STOCK_ENTRY_ISSUE_TYPES:
                    qty_change = -abs(doc.actual_qty)
            except Exception:
                pass

    event_type = _get_voucher_event_type(doc.voucher_type, qty_change)
    base_qty = _get_base_qty(doc.item_code)

    payload = build_stock_payload(
        mapping=mapping,
        qty_change=qty_change,
        voucher_type=doc.voucher_type,
        voucher_no=doc.voucher_no,
        source_site=get_site_url(),
        remarks=f"{doc.voucher_type}: {doc.voucher_no}",
        base_qty=base_qty,
    )
    enqueue_outbox(
        event_type=event_type,
        payload=payload,
        voucher_type=doc.voucher_type,
        voucher_no=doc.voucher_no,
    )


def on_stock_ledger_entry_cancel(doc, method):
    """
    Called when ANY Stock Ledger Entry is cancelled in ERPNext.
    Reverse the stock movement by pushing the opposite event.
    """
    config = get_config()
    if not config or not config.sync_enabled:
        return

    mapping = get_mapping(doc.item_code)
    if not mapping:
        return

    # Reverse the qty_change
    qty_change = -doc.actual_qty

    # _get_voucher_event_type's VOUCHER_TYPE_MAP encodes the *forward*
    # (submit) direction only — a cancelled Purchase Receipt would still
    # look up "stock.receipt" there even though the negated qty_change
    # means it's actually a deduction now. Derive purely from the
    # (already-reversed) sign instead of the fixed per-type mapping.
    if qty_change > 0:
        event_type = "stock.receipt"
    elif qty_change < 0:
        event_type = "stock.deduct"
    else:
        event_type = "stock.adjustment"
    base_qty = _get_base_qty(doc.item_code)

    payload = build_stock_payload(
        mapping=mapping,
        qty_change=qty_change,
        voucher_type=doc.voucher_type,
        voucher_no=f"CANCEL-{doc.voucher_no}",
        source_site=get_site_url(),
        remarks=f"{doc.voucher_type} {doc.voucher_no} cancelled",
        base_qty=base_qty,
    )
    enqueue_outbox(
        event_type=event_type,
        payload=payload,
        voucher_type=f"{doc.voucher_type} Cancel",
        voucher_no=doc.voucher_no,
    )


def _is_saathimart_order_from_si(doc):
    for item in (doc.get("items") or []):
        so = getattr(item, "against_sales_order", None) or getattr(item, "sales_order", None)
        if so and frappe.db.exists("Vendor Order", {"sales_order": so}):
            return True
    return False

app_name = "saathimart_vendor"
app_title = "SaathiMart Vendor"
app_publisher = "Trevo Cloud Nepal"
app_description = "Sync layer between vendor ERPNext and SaathiMart hub"
app_email = "dev@trevo.com.np"
app_license = "mit"
app_version = "0.1.0"

required_apps = ["frappe", "erpnext"]

# ── ERPNext doc event hooks ───────────────────────────────────────────
# Stock Ledger Entry is the single source of truth for ALL stock movements
# in ERPNext. Every stock transaction (Purchase Receipt, Sales Invoice,
# Stock Entry, Delivery Note, Stock Reconciliation, Manufacturing, etc.)
# creates SLE records. Hooking into SLE catches everything.
doc_events = {
    "Stock Ledger Entry": {
        "on_submit": "saathimart_vendor.event_handlers.stock.on_stock_ledger_entry_submit",
        "on_cancel": "saathimart_vendor.event_handlers.stock.on_stock_ledger_entry_cancel",
    },
    "Sales Order": {
        "on_submit": "saathimart_vendor.event_handlers.orders.on_sales_order_submit",
        "on_cancel":  "saathimart_vendor.event_handlers.orders.on_sales_order_cancel",
    },
    "Delivery Note": {
        "on_submit": "saathimart_vendor.event_handlers.orders.on_delivery_note_submit",
        "on_cancel":  "saathimart_vendor.event_handlers.orders.on_delivery_note_cancel",
    },
    "Item Price": {
        "after_insert": "saathimart_vendor.event_handlers.pricing.on_item_price_change",
        "on_update":    "saathimart_vendor.event_handlers.pricing.on_item_price_change",
    },
    # Frappe doesn't fire doc_events (after_insert/on_update) on child-table
    # rows when the parent is saved — Item Barcode rows are inserted via a
    # raw db_insert() with no lifecycle hooks of their own (see
    # frappe/model/document.py's insert()/_save(), which only ever calls
    # run_method() on the document being saved, not its children). So this
    # hooks the parent Item instead and reads doc.barcodes off it.
    "Item": {
        "after_insert": "saathimart_vendor.event_handlers.mapping.on_item_barcode_change",
        "on_update":    "saathimart_vendor.event_handlers.mapping.on_item_barcode_change",
    },
}

# ── Scheduled tasks ───────────────────────────────────────────────────
scheduler_events = {
    "daily": [
        "saathimart_vendor.tasks.archive_old_outbox",
    ],
    "cron": {
        "* * * * *": [
            "saathimart_vendor.tasks.flush_outbox",
        ],
        "*/5 * * * *": [
            "saathimart_vendor.tasks.check_hub_health",
            "saathimart_vendor.tasks.catch_up_with_hub",
        ],
        "*/10 * * * *": [
            # Fires every 10 min, but reconcile_stock() itself only does
            # real work once/hour, in a per-vendor jittered slot — see its
            # docstring. Not a straight hourly cron on purpose.
            "saathimart_vendor.tasks.reconcile_stock",
        ],
    }
}

# ── Desk tile ─────────────────────────────────────────────────────────
add_to_apps_screen = [
    {
        "name": "saathimart_vendor",
        "logo": "/assets/saathimart_vendor/images/logo.svg",
        "title": "SaathiMart Vendor",
        "route": "/app/vendor-order",
        "has_permission": "saathimart_vendor.api.auth.has_permission",
    }
]

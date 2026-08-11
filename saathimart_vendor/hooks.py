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
}

# ── Scheduled tasks ───────────────────────────────────────────────────
scheduler_events = {
    "cron": {
        "* * * * *": [
            "saathimart_vendor.tasks.flush_outbox",
        ],
        "*/5 * * * *": [
            "saathimart_vendor.tasks.check_hub_health",
        ],
        "0 * * * *": [
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

"""
SaathiMart Vendor test suite.

Covers:
  - Vendor Config validation + get_config()
  - Product Mapping (barcode <-> ERPNext Item) + hub sync
  - Sync Outbox: enqueue, flush (success/failure/backoff/dead)
  - Stock hooks: Purchase Receipt / Sales Invoice / Stock Entry -> outbox events
  - Vendor Order lifecycle: receive from hub, accept -> Sales Order, dispatch, deliver, cancel
  - Inbound hub webhook handlers (api/receive.py)
  - Mapping API: bulk_import, sync_all_unmapped, lookup_barcode
  - Hub health check + stock reconciliation orchestration (tasks.py)

Run (inside the vendors container):
    bench --site vendor1.localhost run-tests --app saathimart_vendor

These are unit-level tests: ERPNext hook functions (on_submit/on_cancel) are
invoked directly with a lightweight frappe._dict standing in for the real
submittable document, rather than driving a full Purchase Receipt / Sales
Invoice / Stock Entry through ERPNext's accounting + stock validation. This
keeps the suite fast and independent of Company/Item Price/Stock Settings
configuration, while still exercising the actual hook logic in hooks/stock.py.
"""
import json
import unittest
from unittest.mock import patch, MagicMock

import frappe
from frappe.utils import flt, now_datetime


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_company(name="Vendor Test Co", abbr="VTC"):
    if frappe.db.exists("Company", name):
        return name
    doc = frappe.new_doc("Company")
    doc.company_name = name
    doc.abbr = abbr
    doc.default_currency = "NPR"
    doc.country = "Nepal"
    doc.insert(ignore_permissions=True)
    # Sales Order defaults its `company` field from the global default — make
    # sure it resolves to this test company regardless of what else exists.
    frappe.db.set_default("company", doc.name)
    frappe.db.commit()
    return doc.name


def _ensure_warehouse(company=None):
    company = company or _ensure_company()
    abbr = frappe.db.get_value("Company", company, "abbr")
    wh_name = f"Stores - {abbr}"
    if frappe.db.exists("Warehouse", wh_name):
        return wh_name
    # Company.insert() normally auto-creates default warehouses; fall back
    # to an explicit one if that didn't happen (e.g. warehouse tree missing).
    doc = frappe.new_doc("Warehouse")
    doc.warehouse_name = "Vendor Test Store"
    doc.company = company
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


def _make_item(item_code, item_name=None):
    if frappe.db.exists("Item", item_code):
        return frappe.get_doc("Item", item_code)
    doc = frappe.new_doc("Item")
    doc.item_code = item_code
    doc.item_name = item_name or item_code
    doc.item_group = "All Item Groups"
    doc.stock_uom = "Nos"
    doc.is_stock_item = 1
    doc.insert(ignore_permissions=True)
    return doc


def _configure_vendor(vendor_id="vendor-test-a", hub_url="http://hub.test:8000",
                      warehouse=None, sync_enabled=1, reconciliation_enabled=1):
    doc = frappe.get_single("Vendor Config")
    doc.hub_url = hub_url
    doc.vendor_id = vendor_id
    doc.api_key = "test-api-key"
    doc.api_secret = "test-api-secret"
    doc.sync_enabled = sync_enabled
    doc.reconciliation_enabled = reconciliation_enabled
    doc.default_warehouse = warehouse or _ensure_warehouse()
    doc.lat = 27.7172
    doc.lng = 85.3240
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return doc


def _make_mapping(barcode, item_code, hub_product_id=None, sync_status="Mapped"):
    existing = frappe.db.get_value("Product Mapping", {"barcode": barcode}, "name")
    if existing:
        return frappe.get_doc("Product Mapping", existing)
    _make_item(item_code)
    doc = frappe.new_doc("Product Mapping")
    doc.barcode = barcode
    doc.item_code = item_code
    doc.hub_product_id = hub_product_id or f"SM-PROD-{barcode}"
    doc.hub_sku = barcode
    doc.sync_status = sync_status
    doc.insert(ignore_permissions=True)
    return doc


def _fake_doc(**kwargs):
    """Lightweight stand-in for a Frappe Document for hook unit tests."""
    return frappe._dict(kwargs)


def _fake_item_row(**kwargs):
    return frappe._dict(kwargs)


# ── Test: Vendor Config ────────────────────────────────────────────────────────

class TestVendorConfig(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")

    def test_vendor_id_required(self):
        doc = frappe.get_single("Vendor Config")
        doc.hub_url = "http://hub.test:8000"
        doc.vendor_id = ""
        with self.assertRaises(frappe.ValidationError):
            doc.save(ignore_permissions=True)

    def test_hub_url_trailing_slash_stripped(self):
        doc = frappe.get_single("Vendor Config")
        doc.hub_url = "http://hub.test:8000/"
        doc.vendor_id = "vendor-test-slash"
        doc.default_warehouse = _ensure_warehouse()
        doc.lat = 27.7
        doc.lng = 85.3
        doc.save(ignore_permissions=True)
        self.assertEqual(doc.hub_url, "http://hub.test:8000")

    def test_get_config_returns_none_when_incomplete(self):
        from saathimart_vendor.utils import get_config
        doc = frappe.get_single("Vendor Config")
        doc.hub_url = ""
        doc.vendor_id = ""
        doc.db_update()
        frappe.db.commit()
        self.assertIsNone(get_config())

    def test_get_config_returns_doc_when_complete(self):
        from saathimart_vendor.utils import get_config
        _configure_vendor(vendor_id="vendor-test-config-ok")
        config = get_config()
        self.assertIsNotNone(config)
        self.assertEqual(config.vendor_id, "vendor-test-config-ok")


# ── Test: Product Mapping ──────────────────────────────────────────────────────

class TestProductMapping(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        _configure_vendor()

    def test_barcode_required(self):
        _make_item("VT-ITEM-001")
        doc = frappe.new_doc("Product Mapping")
        doc.item_code = "VT-ITEM-001"
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_duplicate_barcode_rejected(self):
        _make_mapping("8901234500001", "VT-ITEM-002")
        _make_item("VT-ITEM-002B")
        dupe = frappe.new_doc("Product Mapping")
        dupe.barcode = "8901234500001"
        dupe.item_code = "VT-ITEM-002B"
        with self.assertRaises(frappe.DuplicateEntryError):
            dupe.insert(ignore_permissions=True)

    @patch("saathimart_vendor.doctype.product_mapping.product_mapping.hub_get")
    def test_sync_with_hub_success(self, mock_hub_get):
        mock_hub_get.return_value = {"name": "SM-PROD-0001", "sku": "TOMATO-1KG"}
        mapping = _make_mapping("8901234500002", "VT-ITEM-003", sync_status="Unmapped")
        result = mapping.sync_with_hub()
        self.assertEqual(result["hub_product_id"], "SM-PROD-0001")
        mapping.reload()
        self.assertEqual(mapping.sync_status, "Mapped")
        self.assertEqual(mapping.hub_sku, "TOMATO-1KG")

    @patch("saathimart_vendor.doctype.product_mapping.product_mapping.hub_get")
    def test_sync_with_hub_not_found_raises_and_marks_error(self, mock_hub_get):
        mock_hub_get.return_value = None
        mapping = _make_mapping("8901234500003", "VT-ITEM-004", sync_status="Unmapped")
        with self.assertRaises(frappe.ValidationError):
            mapping.sync_with_hub()
        mapping.reload()
        self.assertEqual(mapping.sync_status, "Error")


# ── Test: Sync Outbox — enqueue + flush ────────────────────────────────────────

class TestSyncOutbox(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        _configure_vendor(vendor_id="vendor-test-outbox")
        frappe.db.delete("Sync Outbox", {"voucher_type": "Test Voucher"})
        frappe.db.commit()

    def test_enqueue_outbox_creates_pending_row(self):
        from saathimart_vendor.utils import enqueue_outbox
        enqueue_outbox(
            event_type="stock.receipt",
            payload={"barcode": "123", "qty_change": 5},
            voucher_type="Test Voucher",
            voucher_no="TV-001",
        )
        row = frappe.db.get_value(
            "Sync Outbox", {"voucher_type": "Test Voucher", "voucher_no": "TV-001"},
            ["status", "event_type", "payload"], as_dict=True,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row.status, "Pending")
        self.assertEqual(row.event_type, "stock.receipt")
        self.assertEqual(json.loads(row.payload)["qty_change"], 5)

    @patch("saathimart_vendor.tasks.requests.post")
    def test_flush_outbox_marks_sent_on_success(self, mock_post):
        from saathimart_vendor.utils import enqueue_outbox
        from saathimart_vendor.tasks import flush_outbox

        mock_post.return_value = MagicMock(ok=True, status_code=200)
        enqueue_outbox("order.confirmed", {"order_id": "SM-ORD-1"},
                       voucher_type="Test Voucher", voucher_no="TV-002")
        flush_outbox()
        status = frappe.db.get_value(
            "Sync Outbox", {"voucher_type": "Test Voucher", "voucher_no": "TV-002"}, "status"
        )
        self.assertEqual(status, "Sent")

    @patch("saathimart_vendor.tasks.requests.post")
    def test_flush_outbox_failure_schedules_retry(self, mock_post):
        from saathimart_vendor.utils import enqueue_outbox
        from saathimart_vendor.tasks import flush_outbox

        mock_post.return_value = MagicMock(ok=False, status_code=500, text="server error")
        enqueue_outbox("order.confirmed", {"order_id": "SM-ORD-2"},
                       voucher_type="Test Voucher", voucher_no="TV-003")
        flush_outbox()
        row = frappe.db.get_value(
            "Sync Outbox", {"voucher_type": "Test Voucher", "voucher_no": "TV-003"},
            ["status", "retry_count", "next_retry_at"], as_dict=True,
        )
        self.assertEqual(row.status, "Pending")
        self.assertEqual(row.retry_count, 1)
        self.assertIsNotNone(row.next_retry_at)

    @patch("saathimart_vendor.tasks.frappe.sendmail")
    @patch("saathimart_vendor.tasks.requests.post")
    def test_flush_outbox_marks_dead_after_ten_retries(self, mock_post, mock_sendmail):
        from saathimart_vendor.utils import enqueue_outbox
        from saathimart_vendor.tasks import flush_outbox

        frappe.db.set_single_value("Vendor Config", "admin_email", "ops@example.com")
        mock_post.return_value = MagicMock(ok=False, status_code=500, text="still failing")
        enqueue_outbox("order.confirmed", {"order_id": "SM-ORD-3"},
                       voucher_type="Test Voucher", voucher_no="TV-004")
        name = frappe.db.get_value(
            "Sync Outbox", {"voucher_type": "Test Voucher", "voucher_no": "TV-004"}, "name"
        )
        frappe.db.set_value("Sync Outbox", name, "retry_count", 10)
        flush_outbox()
        row = frappe.db.get_value("Sync Outbox", name, ["status", "retry_count"], as_dict=True)
        self.assertEqual(row.status, "Dead")
        self.assertEqual(row.retry_count, 11)
        mock_sendmail.assert_called_once()

    @patch("saathimart_vendor.tasks.requests.post")
    def test_flush_outbox_skips_when_sync_disabled(self, mock_post):
        from saathimart_vendor.utils import enqueue_outbox
        from saathimart_vendor.tasks import flush_outbox

        _configure_vendor(vendor_id="vendor-test-outbox", sync_enabled=0)
        enqueue_outbox("order.confirmed", {"order_id": "SM-ORD-4"},
                       voucher_type="Test Voucher", voucher_no="TV-005")
        flush_outbox()
        mock_post.assert_not_called()
        status = frappe.db.get_value(
            "Sync Outbox", {"voucher_type": "Test Voucher", "voucher_no": "TV-005"}, "status"
        )
        self.assertEqual(status, "Pending")
        _configure_vendor(vendor_id="vendor-test-outbox", sync_enabled=1)


# ── Test: Stock hooks ───────────────────────────────────────────────────────────

class TestStockHooks(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        _configure_vendor(vendor_id="vendor-test-stock")
        self.mapping = _make_mapping("8901234500010", "VT-ITEM-STOCK")
        frappe.db.delete("Sync Outbox", {"voucher_no": ["like", "STOCK-TEST%"]})
        frappe.db.commit()

    def _last_outbox_payload(self, voucher_no):
        row = frappe.db.get_value(
            "Sync Outbox", {"voucher_no": voucher_no}, ["event_type", "payload"], as_dict=True
        )
        self.assertIsNotNone(row, f"no outbox row for voucher {voucher_no}")
        return row.event_type, json.loads(row.payload)

    def test_purchase_receipt_submit_enqueues_stock_receipt(self):
        from saathimart_vendor.event_handlers.stock import on_purchase_receipt_submit
        doc = _fake_doc(name="STOCK-TEST-PR-1", items=[
            _fake_item_row(item_code="VT-ITEM-STOCK", qty=15),
        ])
        on_purchase_receipt_submit(doc, "on_submit")
        event_type, payload = self._last_outbox_payload("STOCK-TEST-PR-1")
        self.assertEqual(event_type, "stock.receipt")
        self.assertEqual(payload["qty_change"], 15)
        self.assertEqual(payload["barcode"], "8901234500010")

    def test_purchase_receipt_cancel_enqueues_stock_deduct(self):
        from saathimart_vendor.event_handlers.stock import on_purchase_receipt_cancel
        doc = _fake_doc(name="STOCK-TEST-PR-2", items=[
            _fake_item_row(item_code="VT-ITEM-STOCK", qty=5),
        ])
        on_purchase_receipt_cancel(doc, "on_cancel")
        event_type, payload = self._last_outbox_payload("CANCEL-STOCK-TEST-PR-2")
        self.assertEqual(event_type, "stock.deduct")
        self.assertEqual(payload["qty_change"], -5)

    def test_purchase_receipt_unmapped_item_creates_no_outbox_row(self):
        from saathimart_vendor.event_handlers.stock import on_purchase_receipt_submit
        _make_item("VT-ITEM-UNMAPPED")
        doc = _fake_doc(name="STOCK-TEST-PR-3", items=[
            _fake_item_row(item_code="VT-ITEM-UNMAPPED", qty=10),
        ])
        on_purchase_receipt_submit(doc, "on_submit")
        count = frappe.db.count("Sync Outbox", {"voucher_no": "STOCK-TEST-PR-3"})
        self.assertEqual(count, 0)

    def test_sales_invoice_submit_enqueues_stock_deduct(self):
        from saathimart_vendor.event_handlers.stock import on_sales_invoice_submit
        doc = _fake_doc(name="STOCK-TEST-SI-1", items=[
            _fake_item_row(item_code="VT-ITEM-STOCK", qty=3, sales_order=None),
        ])
        on_sales_invoice_submit(doc, "on_submit")
        event_type, payload = self._last_outbox_payload("STOCK-TEST-SI-1")
        self.assertEqual(event_type, "stock.deduct")
        self.assertEqual(payload["qty_change"], -3)

    def test_sales_invoice_skipped_for_saathimart_order(self):
        from saathimart_vendor.event_handlers.stock import on_sales_invoice_submit
        so_name = "SO-SM-LINKED-001"
        if not frappe.db.exists("Vendor Order", "HUB-ORDER-LINKED-001"):
            vo = frappe.new_doc("Vendor Order")
            vo.hub_order_id = "HUB-ORDER-LINKED-001"
            vo.sales_order = so_name
            vo.customer_name = "Test Customer"
            vo.items = []
            vo.received_at = now_datetime()
            vo.insert(ignore_permissions=True)

        doc = _fake_doc(name="STOCK-TEST-SI-2", voucher_type="Sales Invoice", items=[
            _fake_item_row(item_code="VT-ITEM-STOCK", qty=2, sales_order=so_name),
        ])
        on_sales_invoice_submit(doc, "on_submit")
        count = frappe.db.count("Sync Outbox", {"voucher_no": "STOCK-TEST-SI-2"})
        self.assertEqual(count, 0)

    def test_stock_entry_material_receipt_increases_qty(self):
        from saathimart_vendor.event_handlers.stock import on_stock_entry_submit
        doc = _fake_doc(name="STOCK-TEST-SE-1", stock_entry_type="Material Receipt", items=[
            _fake_item_row(item_code="VT-ITEM-STOCK", qty=8),
        ])
        on_stock_entry_submit(doc, "on_submit")
        event_type, payload = self._last_outbox_payload("STOCK-TEST-SE-1")
        self.assertEqual(event_type, "stock.receipt")
        self.assertEqual(payload["qty_change"], 8)

    def test_stock_entry_material_issue_decreases_qty(self):
        from saathimart_vendor.event_handlers.stock import on_stock_entry_submit
        doc = _fake_doc(name="STOCK-TEST-SE-2", stock_entry_type="Material Issue", items=[
            _fake_item_row(item_code="VT-ITEM-STOCK", qty=4),
        ])
        on_stock_entry_submit(doc, "on_submit")
        event_type, payload = self._last_outbox_payload("STOCK-TEST-SE-2")
        self.assertEqual(event_type, "stock.deduct")
        self.assertEqual(payload["qty_change"], -4)

    def test_stock_entry_repack_ignored_types_produce_no_row(self):
        from saathimart_vendor.event_handlers.stock import on_stock_entry_submit
        doc = _fake_doc(name="STOCK-TEST-SE-3", stock_entry_type="Send to Subcontractor", items=[
            _fake_item_row(item_code="VT-ITEM-STOCK", qty=1),
        ])
        on_stock_entry_submit(doc, "on_submit")
        count = frappe.db.count("Sync Outbox", {"voucher_no": "STOCK-TEST-SE-3"})
        self.assertEqual(count, 0)


# ── Test: Inbound webhook handlers (api/receive.py) ────────────────────────────

class TestReceiveFromHub(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        _configure_vendor(vendor_id="vendor-test-receive")

    def _clear_request_secret(self):
        doc = frappe.get_single("Vendor Config")
        doc.webhook_secret = ""
        doc.save(ignore_permissions=True)

    def test_handle_new_order_creates_vendor_order(self):
        from saathimart_vendor.api.receive import _handle_new_order
        hub_order_id = "HUB-ORDER-NEW-001"
        frappe.db.delete("Vendor Order", {"hub_order_id": hub_order_id})
        frappe.db.commit()

        _handle_new_order({
            "order_id": hub_order_id,
            "customer_name": "Ram Shrestha",
            "customer_phone": "9800000000",
            "delivery_address": "Baneshwor, Kathmandu",
            "grand_total": 450,
            "payment_method": "COD",
            "items": [{"product": "SM-PROD-0001", "qty": 2, "rate": 100}],
        })
        doc = frappe.get_doc("Vendor Order", hub_order_id)
        self.assertEqual(doc.status, "Received")
        self.assertEqual(doc.customer_name, "Ram Shrestha")
        self.assertEqual(flt(doc.grand_total), 450)
        self.assertEqual(len(doc.items), 1)

    def test_handle_new_order_idempotent_on_duplicate(self):
        from saathimart_vendor.api.receive import _handle_new_order
        hub_order_id = "HUB-ORDER-DUPE-001"
        frappe.db.delete("Vendor Order", {"hub_order_id": hub_order_id})
        frappe.db.commit()
        payload = {"order_id": hub_order_id, "customer_name": "A", "grand_total": 100, "items": []}
        _handle_new_order(payload)
        _handle_new_order(payload)  # second push for same order must not error/duplicate
        count = frappe.db.count("Vendor Order", {"hub_order_id": hub_order_id})
        self.assertEqual(count, 1)

    def test_handle_order_cancel_updates_status(self):
        from saathimart_vendor.api.receive import _handle_new_order, _handle_order_cancel
        hub_order_id = "HUB-ORDER-CANCEL-001"
        frappe.db.delete("Vendor Order", {"hub_order_id": hub_order_id})
        frappe.db.commit()
        _handle_new_order({"order_id": hub_order_id, "customer_name": "A", "grand_total": 100, "items": []})
        _handle_order_cancel({"order_id": hub_order_id, "reason": "Customer changed mind"})
        status = frappe.db.get_value("Vendor Order", hub_order_id, "status")
        self.assertEqual(status, "Cancelled")

    def test_handle_order_cancel_skips_already_delivered(self):
        from saathimart_vendor.api.receive import _handle_new_order, _handle_order_cancel
        hub_order_id = "HUB-ORDER-DELIVERED-001"
        frappe.db.delete("Vendor Order", {"hub_order_id": hub_order_id})
        frappe.db.commit()
        _handle_new_order({"order_id": hub_order_id, "customer_name": "A", "grand_total": 100, "items": []})
        frappe.db.set_value("Vendor Order", hub_order_id, "status", "Delivered")
        _handle_order_cancel({"order_id": hub_order_id, "reason": "too late"})
        status = frappe.db.get_value("Vendor Order", hub_order_id, "status")
        self.assertEqual(status, "Delivered")  # unchanged

    def test_handle_order_reassign_marks_cancelled(self):
        from saathimart_vendor.api.receive import _handle_new_order, _handle_order_reassign
        hub_order_id = "HUB-ORDER-REASSIGN-001"
        frappe.db.delete("Vendor Order", {"hub_order_id": hub_order_id})
        frappe.db.commit()
        _handle_new_order({"order_id": hub_order_id, "customer_name": "A", "grand_total": 100, "items": []})
        _handle_order_reassign({"order_id": hub_order_id, "reason": "vendor out of stock"})
        status = frappe.db.get_value("Vendor Order", hub_order_id, "status")
        self.assertEqual(status, "Cancelled")

    def test_receive_from_hub_requires_event(self):
        from saathimart_vendor.api.receive import receive_from_hub
        self._clear_request_secret()
        with self.assertRaises(frappe.ValidationError):
            receive_from_hub(event=None, payload={})

    def test_receive_from_hub_unknown_event_does_not_raise(self):
        from saathimart_vendor.api.receive import receive_from_hub
        self._clear_request_secret()
        result = receive_from_hub(event="order.teleported", payload={})
        self.assertEqual(result, {"ok": True})


# ── Test: Vendor Order lifecycle ────────────────────────────────────────────────

class TestVendorOrderLifecycle(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.config = _configure_vendor(vendor_id="vendor-test-lifecycle")
        self.mapping = _make_mapping("8901234500020", "VT-ITEM-ORDER")

    def _make_received_order(self, hub_order_id, items=None):
        frappe.db.delete("Vendor Order", {"hub_order_id": hub_order_id})
        frappe.db.commit()
        doc = frappe.new_doc("Vendor Order")
        doc.hub_order_id = hub_order_id
        doc.status = "Received"
        doc.customer_name = "Sita Gurung"
        doc.customer_phone = "9811111111"
        doc.grand_total = 500
        doc.received_at = now_datetime()
        for it in (items if items is not None else [
            {"product": self.mapping.hub_product_id, "qty": 2, "rate": 100},
        ]):
            doc.append("items", {
                "product": it.get("product", ""),
                "item_code": it.get("item_code", it.get("product", "")),
                "qty": it.get("qty", 1),
                "rate": it.get("rate", 0),
            })
        doc.insert(ignore_permissions=True)
        return doc

    def test_accept_order_creates_and_submits_sales_order(self):
        vo = self._make_received_order("HUB-ORDER-ACCEPT-001")
        result = vo.accept_order()
        self.assertTrue(frappe.db.exists("Sales Order", result["sales_order"]))
        so = frappe.get_doc("Sales Order", result["sales_order"])
        self.assertEqual(so.docstatus, 1)
        vo.reload()
        self.assertEqual(vo.status, "Accepted")
        self.assertIsNotNone(vo.accepted_at)

    def test_accept_order_wrong_status_raises(self):
        vo = self._make_received_order("HUB-ORDER-ACCEPT-002")
        vo.db_set("status", "Dispatched")
        vo.reload()
        with self.assertRaises(frappe.ValidationError):
            vo.accept_order()

    def test_accept_order_missing_mapping_raises(self):
        vo = self._make_received_order("HUB-ORDER-ACCEPT-003", items=[
            {"product": "SM-PROD-NOT-MAPPED", "qty": 1, "rate": 50},
        ])
        with self.assertRaises(frappe.ValidationError):
            vo.accept_order()

    def test_accept_order_empty_items_raises(self):
        vo = self._make_received_order("HUB-ORDER-ACCEPT-004", items=[])
        with self.assertRaises(frappe.ValidationError):
            vo.accept_order()

    def test_mark_dispatched_enqueues_event_and_sets_timestamp(self):
        vo = self._make_received_order("HUB-ORDER-DISPATCH-001")
        vo.accept_order()
        vo.reload()
        vo.mark_dispatched()
        vo.reload()
        self.assertEqual(vo.status, "Dispatched")
        self.assertIsNotNone(vo.dispatched_at)
        event_type = frappe.db.get_value(
            "Sync Outbox", {"voucher_no": vo.name, "event_type": "order.dispatched"}, "event_type"
        )
        self.assertEqual(event_type, "order.dispatched")

    def test_mark_dispatched_wrong_status_raises(self):
        vo = self._make_received_order("HUB-ORDER-DISPATCH-002")
        with self.assertRaises(frappe.ValidationError):
            vo.mark_dispatched()  # still "Received", not Accepted/Preparing

    def test_mark_delivered_after_dispatch(self):
        vo = self._make_received_order("HUB-ORDER-DELIVER-001")
        vo.accept_order()
        vo.reload()
        vo.mark_dispatched()
        vo.reload()
        vo.mark_delivered()
        vo.reload()
        self.assertEqual(vo.status, "Delivered")
        self.assertIsNotNone(vo.delivered_at)

    def test_mark_delivered_without_dispatch_raises(self):
        vo = self._make_received_order("HUB-ORDER-DELIVER-002")
        with self.assertRaises(frappe.ValidationError):
            vo.mark_delivered()

    def test_cancel_order_before_accept(self):
        vo = self._make_received_order("HUB-ORDER-CANCEL-VENDOR-001")
        vo.cancel_order(reason="Out of stock")
        vo.reload()
        self.assertEqual(vo.status, "Cancelled")
        self.assertIn("Out of stock", vo.notes)

    def test_cancel_order_after_accept_cancels_sales_order(self):
        vo = self._make_received_order("HUB-ORDER-CANCEL-VENDOR-002")
        vo.accept_order()
        vo.reload()
        so_name = vo.sales_order
        vo.cancel_order(reason="Customer request")
        so = frappe.get_doc("Sales Order", so_name)
        self.assertEqual(so.docstatus, 2)

    def test_cancel_order_when_delivered_raises(self):
        vo = self._make_received_order("HUB-ORDER-CANCEL-VENDOR-003")
        vo.db_set("status", "Delivered")
        vo.reload()
        with self.assertRaises(frappe.ValidationError):
            vo.cancel_order()


# ── Test: Mapping API (bulk import / sync) ──────────────────────────────────────

class TestMappingAPI(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        _configure_vendor(vendor_id="vendor-test-mapping-api")

    @patch("saathimart_vendor.api.mapping.hub_get")
    def test_lookup_barcode_found(self, mock_hub_get):
        from saathimart_vendor.api.mapping import lookup_barcode
        mock_hub_get.return_value = {"name": "SM-PROD-0002", "sku": "RICE-5KG", "price": 650}
        result = lookup_barcode("8901234500030")
        self.assertTrue(result["found"])
        self.assertEqual(result["name"], "SM-PROD-0002")

    @patch("saathimart_vendor.api.mapping.hub_get")
    def test_lookup_barcode_not_found(self, mock_hub_get):
        from saathimart_vendor.api.mapping import lookup_barcode
        mock_hub_get.return_value = None
        result = lookup_barcode("0000000000000")
        self.assertFalse(result["found"])

    @patch("saathimart_vendor.api.mapping.hub_get")
    def test_bulk_import_creates_mappings(self, mock_hub_get):
        from saathimart_vendor.api.mapping import bulk_import
        _make_item("VT-ITEM-BULK-1")
        mock_hub_get.return_value = {"name": "SM-PROD-0003", "sku": "OIL-1L"}
        frappe.db.delete("Product Mapping", {"barcode": "8901234500040"})
        frappe.db.commit()

        csv_content = "item_code,barcode\nVT-ITEM-BULK-1,8901234500040\n"
        result = bulk_import(csv_content)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["errors"], [])
        mapping = frappe.db.get_value(
            "Product Mapping", {"barcode": "8901234500040"}, ["sync_status"], as_dict=True
        )
        self.assertEqual(mapping.sync_status, "Mapped")

    @patch("saathimart_vendor.api.mapping.hub_get")
    def test_bulk_import_skips_existing_barcode(self, mock_hub_get):
        from saathimart_vendor.api.mapping import bulk_import
        mock_hub_get.return_value = {"name": "SM-PROD-0004", "sku": "SUGAR-1KG"}
        _make_mapping("8901234500050", "VT-ITEM-BULK-2")

        csv_content = "item_code,barcode\nVT-ITEM-BULK-2,8901234500050\n"
        result = bulk_import(csv_content)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["created"], 0)

    def test_bulk_import_missing_columns_recorded_as_error(self):
        from saathimart_vendor.api.mapping import bulk_import
        csv_content = "item_code,barcode\n,8901234500060\n"
        result = bulk_import(csv_content)
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["errors"]), 1)

    @patch("saathimart_vendor.api.mapping.hub_get")
    def test_sync_all_unmapped_fixes_mapped(self, mock_hub_get):
        from saathimart_vendor.api.mapping import sync_all_unmapped
        mock_hub_get.return_value = {"name": "SM-PROD-0005", "sku": "LENTIL-1KG"}
        _make_mapping("8901234500070", "VT-ITEM-BULK-3", sync_status="Unmapped")

        result = sync_all_unmapped()
        self.assertGreaterEqual(result["fixed"], 1)
        status = frappe.db.get_value("Product Mapping", {"barcode": "8901234500070"}, "sync_status")
        self.assertEqual(status, "Mapped")


# ── Test: tasks.py orchestration ────────────────────────────────────────────────

class TestTasks(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.config = _configure_vendor(vendor_id="vendor-test-tasks")

    @patch("saathimart_vendor.tasks.requests.get")
    def test_check_hub_health_marks_active(self, mock_get):
        from saathimart_vendor.tasks import check_hub_health
        mock_get.return_value = MagicMock(ok=True, status_code=200)
        check_hub_health()
        status = frappe.db.get_single_value("Vendor Config", "hub_status")
        self.assertEqual(status, "Active")

    @patch("saathimart_vendor.tasks.requests.get")
    def test_check_hub_health_marks_unreachable_on_exception(self, mock_get):
        from saathimart_vendor.tasks import check_hub_health
        mock_get.side_effect = ConnectionError("no route to host")
        check_hub_health()
        status = frappe.db.get_single_value("Vendor Config", "hub_status")
        self.assertEqual(status, "Unreachable")

    @patch("saathimart_vendor.tasks._reconcile_item")
    def test_reconcile_stock_visits_every_active_mapping(self, mock_reconcile_item):
        from saathimart_vendor.tasks import reconcile_stock
        _make_mapping("8901234500080", "VT-ITEM-RECON-1")
        _make_mapping("8901234500081", "VT-ITEM-RECON-2")
        reconcile_stock()
        self.assertGreaterEqual(mock_reconcile_item.call_count, 2)

    @patch("saathimart_vendor.tasks._reconcile_item")
    def test_reconcile_stock_skips_when_disabled(self, mock_reconcile_item):
        from saathimart_vendor.tasks import reconcile_stock
        _configure_vendor(vendor_id="vendor-test-tasks", reconciliation_enabled=0)
        reconcile_stock()
        mock_reconcile_item.assert_not_called()
        _configure_vendor(vendor_id="vendor-test-tasks", reconciliation_enabled=1)


if __name__ == "__main__":
    unittest.main()

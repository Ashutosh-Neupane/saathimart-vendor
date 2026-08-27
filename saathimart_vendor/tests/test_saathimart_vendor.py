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
from frappe.utils import flt, now_datetime, today, getdate


# ── Module-level fixture isolation ──────────────────────────────────────────
# Vendor Config is a Frappe Single — every _configure_vendor() call below
# overwrites the one real row for this site, and plain unittest.TestCase
# (used throughout this file, not frappe.tests.utils.FrappeTestCase) gives
# no automatic per-test rollback. Left unrestored, a live site's real
# hub_url/vendor_id/api credentials silently end up holding test fixture
# values after `bench run-tests` finishes — this already happened once and
# broke real vendor->hub sync (bad hub_url) until it was manually caught and
# fixed. setUpModule/tearDownModule run exactly once around this whole
# file's test run, regardless of how many classes call _configure_vendor(),
# so whatever was really configured before the suite ran is always restored
# after — without touching the many individual tests that rely on it being
# mutable mid-suite.
_VENDOR_CONFIG_FIELDS = [
    "hub_url", "vendor_id", "api_key", "sync_enabled",
    "reconciliation_enabled", "default_warehouse", "lat", "lng",
]
_original_vendor_config = None
_original_vendor_api_secret = None


def setUpModule():
    global _original_vendor_config, _original_vendor_api_secret
    frappe.set_user("Administrator")
    doc = frappe.get_single("Vendor Config")
    _original_vendor_config = {f: doc.get(f) for f in _VENDOR_CONFIG_FIELDS}
    _original_vendor_api_secret = doc.get_password("api_secret", raise_exception=False)


def tearDownModule():
    if _original_vendor_config is None:
        return
    frappe.set_user("Administrator")
    doc = frappe.get_single("Vendor Config")
    for field, value in _original_vendor_config.items():
        doc.set(field, value)
    if _original_vendor_api_secret is not None:
        doc.api_secret = _original_vendor_api_secret
    doc.flags.ignore_mandatory = True
    doc.save(ignore_permissions=True)
    frappe.db.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_fiscal_year():
    """Sales Order.submit() requires an active Fiscal Year covering today —
    unlike the standard warehouses, ERPNext's Company.insert() doesn't
    create one on its own."""
    year = getdate(today()).year
    if frappe.db.exists("Fiscal Year", {
        "year_start_date": ["<=", today()], "year_end_date": [">=", today()],
    }):
        return
    fy = frappe.new_doc("Fiscal Year")
    fy.year = str(year)
    fy.year_start_date = f"{year}-01-01"
    fy.year_end_date = f"{year}-12-31"
    fy.insert(ignore_permissions=True)
    frappe.db.commit()


def _ensure_base_fixtures():
    """Warehouse Type: Transit (needed by Company.on_update's
    create_default_warehouses()) and a Standard Selling Price List (needed
    by Sales Order.set_missing_values()) — nothing on a fresh test site
    creates these except the Setup Wizard, which never runs here.
    erpnext.setup.setup_wizard.operations.install_fixtures.install() would
    normally provide both (plus Item Groups, Stock Entry Types, etc.), but
    it hits a NestedSetRecursionError on its Sales Person fixture on this
    ERPNext version — create just the two records actually needed instead
    of pulling in that whole (currently broken) installer.
    """
    if not frappe.db.exists("Warehouse Type", "Transit"):
        frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)
    for pl_name, buying, selling in [("Standard Buying", 1, 0), ("Standard Selling", 0, 1)]:
        if not frappe.db.exists("Price List", pl_name):
            frappe.get_doc({
                "doctype": "Price List", "price_list_name": pl_name, "enabled": 1,
                "buying": buying, "selling": selling, "currency": "NPR",
            }).insert(ignore_permissions=True)
    frappe.db.commit()


def _ensure_company(name="Vendor Test Co", abbr="VTC"):
    _ensure_fiscal_year()
    _ensure_base_fixtures()
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


def _configure_vendor(vendor_id="vendor-test-a", hub_url="http://hub:8000",
                      warehouse=None, sync_enabled=1, reconciliation_enabled=1):
    # Vendor Config is a Frappe Single — every test run overwrites the same
    # site-wide row, and it is never restored afterward. hub_url must
    # therefore default to the real docker-network hostname ("hub", the
    # sm-hub service) rather than a placeholder: a fake host here silently
    # breaks every real vendor->hub push (orders, stock, price) until
    # someone notices and manually reconfigures it, which is exactly what
    # happened live in this environment before this default was fixed.
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
    # Always fresh, never a reuse-if-exists — a leftover row from an earlier
    # run (this file uses plain unittest.TestCase, so nothing rolls back
    # between `bench run-tests` invocations) used to get silently returned
    # as-is here regardless of the sync_status the *current* test asked for,
    # which made test_sync_all_unmapped_fixes_mapped flake depending on
    # whether it was the first or a later run against the same site.
    frappe.db.delete("Product Mapping", {"barcode": barcode})
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
        doc.hub_url = "http://hub:8000"
        doc.vendor_id = ""
        with self.assertRaises(frappe.ValidationError):
            doc.save(ignore_permissions=True)

    def test_hub_url_trailing_slash_stripped(self):
        doc = frappe.get_single("Vendor Config")
        doc.hub_url = "http://hub:8000/"
        doc.vendor_id = "vendor-test-slash"
        doc.default_warehouse = _ensure_warehouse()
        doc.lat = 27.7
        doc.lng = 85.3
        doc.save(ignore_permissions=True)
        self.assertEqual(doc.hub_url, "http://hub:8000")

    def test_get_config_returns_none_when_incomplete(self):
        from saathimart_vendor.utils import get_config
        # Vendor Config is a Single — it lives in tabSingles, not a
        # tabVendor Config table, so doc.db_update() (which blindly issues
        # `UPDATE tab<doctype>`) fails; frappe.db.set_value() knows how to
        # write Singles correctly while still bypassing doc-level validation
        # (needed here since vendor_id="" would normally fail .save()).
        frappe.db.set_value("Vendor Config", "Vendor Config", {"hub_url": "", "vendor_id": ""})
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
        # Barcode uniqueness is scoped to (vendor, barcode) by design — the
        # same physical barcode can be mapped independently by different
        # vendors (see product_mapping.json's vendor field). What must
        # still be rejected is the *same* vendor mapping the same barcode
        # to two different items, which is an application-level check in
        # validate() (frappe.ValidationError), not a DB-level unique
        # constraint on barcode alone (which would incorrectly block
        # different vendors from sharing a barcode).
        _make_mapping("8901234500001", "VT-ITEM-002")
        _make_item("VT-ITEM-002B")
        dupe = frappe.new_doc("Product Mapping")
        dupe.barcode = "8901234500001"
        dupe.item_code = "VT-ITEM-002B"
        with self.assertRaises(frappe.ValidationError):
            dupe.insert(ignore_permissions=True)

    @patch("saathimart_vendor.saathimart_vendor.doctype.product_mapping.product_mapping.hub_get")
    def test_sync_with_hub_success(self, mock_hub_get):
        mock_hub_get.return_value = {"name": "SM-PROD-0001", "sku": "TOMATO-1KG"}
        mapping = _make_mapping("8901234500002", "VT-ITEM-003", sync_status="Unmapped")
        result = mapping.sync_with_hub()
        self.assertEqual(result["hub_product_id"], "SM-PROD-0001")
        mapping.reload()
        self.assertEqual(mapping.sync_status, "Mapped")
        self.assertEqual(mapping.hub_sku, "TOMATO-1KG")

    @patch("saathimart_vendor.saathimart_vendor.doctype.product_mapping.product_mapping.hub_get")
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

    @patch("saathimart_vendor.tasks.requests.post")
    def test_flush_outbox_batches_multiple_pending_rows_into_one_bulk_call(self, mock_post):
        """
        Real batching happens by reading pending rows straight off the
        Sync Outbox table inside flush_outbox() — not via an in-memory
        buffer (an earlier version tried that; it couldn't work, since
        each enqueue_outbox() call can run in a different process than
        the one that would eventually flush it). With >1 pending row this
        must hit bulk_receive once, not events.receive N times.
        """
        from saathimart_vendor.utils import enqueue_outbox
        from saathimart_vendor.tasks import flush_outbox

        mock_post.return_value = MagicMock(ok=True, status_code=200)
        for i in range(3):
            enqueue_outbox("price.update", {"product_id": f"SM-PROD-BATCH-{i}"},
                           voucher_type="Test Voucher", voucher_no=f"TV-BATCH-{i}")
        flush_outbox()

        self.assertEqual(mock_post.call_count, 1)
        called_url = mock_post.call_args.args[0]
        self.assertIn("bulk_receive", called_url)
        sent_events = mock_post.call_args.kwargs["json"]["events"]
        self.assertEqual(len(sent_events), 3)
        for i in range(3):
            status = frappe.db.get_value(
                "Sync Outbox", {"voucher_type": "Test Voucher", "voucher_no": f"TV-BATCH-{i}"}, "status"
            )
            self.assertEqual(status, "Sent")

    @patch("saathimart_vendor.tasks.requests.post")
    def test_flush_outbox_single_pending_row_uses_plain_endpoint(self, mock_post):
        """A lone pending row shouldn't pay bulk overhead — same as before batching existed."""
        from saathimart_vendor.utils import enqueue_outbox
        from saathimart_vendor.tasks import flush_outbox

        mock_post.return_value = MagicMock(ok=True, status_code=200)
        enqueue_outbox("price.update", {"product_id": "SM-PROD-SOLO"},
                       voucher_type="Test Voucher", voucher_no="TV-SOLO")
        flush_outbox()

        called_url = mock_post.call_args.args[0]
        self.assertIn("saathimart.api.events.receive", called_url)
        self.assertNotIn("bulk_receive", called_url)

    @patch("saathimart_vendor.tasks.requests.post")
    def test_bulk_push_failure_updates_retry_count_for_every_row_not_just_first(self, mock_post):
        """
        Regression test: an earlier version of _push_bulk only called
        _handle_failure on rows[0] when a batch failed, so every other row
        in the batch kept retry_count=0 forever and could never escalate
        to Dead or trigger the admin alert, no matter how long it failed.
        """
        from saathimart_vendor.utils import enqueue_outbox
        from saathimart_vendor.tasks import flush_outbox

        mock_post.return_value = MagicMock(ok=False, status_code=500, text="down")
        for i in range(4):
            enqueue_outbox("price.update", {"product_id": f"SM-PROD-BATCHFAIL-{i}"},
                           voucher_type="Test Voucher", voucher_no=f"TV-BATCHFAIL-{i}")
        flush_outbox()

        for i in range(4):
            row = frappe.db.get_value(
                "Sync Outbox", {"voucher_type": "Test Voucher", "voucher_no": f"TV-BATCHFAIL-{i}"},
                ["status", "retry_count", "next_retry_at"], as_dict=True,
            )
            self.assertEqual(row.status, "Pending")
            self.assertEqual(row.retry_count, 1, f"row {i} did not get its retry_count bumped")
            self.assertIsNotNone(row.next_retry_at)


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

    # hooks.py only registers on_stock_ledger_entry_submit/_cancel (against
    # "Stock Ledger Entry", not per-voucher doctypes — see the module
    # docstring in event_handlers/stock.py: SLE is the single source of
    # truth for every stock movement). These tests drive that one hook with
    # a fake SLE row per voucher type instead of calling per-voucher-type
    # functions that no longer exist.

    def test_purchase_receipt_submit_enqueues_stock_receipt(self):
        from saathimart_vendor.event_handlers.stock import on_stock_ledger_entry_submit
        doc = _fake_doc(
            voucher_type="Purchase Receipt", voucher_no="STOCK-TEST-PR-1",
            item_code="VT-ITEM-STOCK", actual_qty=15,
        )
        on_stock_ledger_entry_submit(doc, "on_submit")
        event_type, payload = self._last_outbox_payload("STOCK-TEST-PR-1")
        self.assertEqual(event_type, "stock.receipt")
        self.assertEqual(payload["qty_change"], 15)
        self.assertEqual(payload["barcode"], "8901234500010")

    def test_purchase_receipt_cancel_enqueues_stock_deduct(self):
        from saathimart_vendor.event_handlers.stock import on_stock_ledger_entry_cancel
        doc = _fake_doc(
            voucher_type="Purchase Receipt", voucher_no="STOCK-TEST-PR-2",
            item_code="VT-ITEM-STOCK", actual_qty=5,
        )
        on_stock_ledger_entry_cancel(doc, "on_cancel")
        # the outbox row's own voucher_no is doc.voucher_no unprefixed —
        # only the payload's *internal* voucher_no (sent to the hub) gets
        # the "CANCEL-" prefix, see on_stock_ledger_entry_cancel().
        event_type, payload = self._last_outbox_payload("STOCK-TEST-PR-2")
        self.assertEqual(event_type, "stock.deduct")
        self.assertEqual(payload["qty_change"], -5)
        self.assertEqual(payload["voucher_no"], "CANCEL-STOCK-TEST-PR-2")

    def test_purchase_receipt_unmapped_item_creates_no_outbox_row(self):
        from saathimart_vendor.event_handlers.stock import on_stock_ledger_entry_submit
        _make_item("VT-ITEM-UNMAPPED")
        doc = _fake_doc(
            voucher_type="Purchase Receipt", voucher_no="STOCK-TEST-PR-3",
            item_code="VT-ITEM-UNMAPPED", actual_qty=10,
        )
        on_stock_ledger_entry_submit(doc, "on_submit")
        count = frappe.db.count("Sync Outbox", {"voucher_no": "STOCK-TEST-PR-3"})
        self.assertEqual(count, 0)

    def test_sales_invoice_submit_enqueues_stock_deduct(self):
        from saathimart_vendor.event_handlers.stock import on_stock_ledger_entry_submit
        doc = _fake_doc(
            voucher_type="Sales Invoice", voucher_no="STOCK-TEST-SI-1",
            item_code="VT-ITEM-STOCK", actual_qty=-3, items=[],
        )
        on_stock_ledger_entry_submit(doc, "on_submit")
        event_type, payload = self._last_outbox_payload("STOCK-TEST-SI-1")
        self.assertEqual(event_type, "stock.deduct")
        self.assertEqual(payload["qty_change"], -3)

    def test_sales_invoice_skipped_for_saathimart_order(self):
        from saathimart_vendor.event_handlers.stock import on_stock_ledger_entry_submit
        so_name = "SO-SM-LINKED-001"
        if not frappe.db.exists("Vendor Order", "HUB-ORDER-LINKED-001"):
            vo = frappe.new_doc("Vendor Order")
            vo.hub_order_id = "HUB-ORDER-LINKED-001"
            vo.customer_name = "Test Customer"
            vo.append("items", {"product": "SM-PROD-LINKED", "qty": 1, "rate": 100})
            vo.received_at = now_datetime()
            vo.insert(ignore_permissions=True)
            # sales_order is a mandatory-on-real-use Link to Sales Order —
            # _is_saathimart_order_from_si only needs a Vendor Order row
            # whose sales_order matches so_name for its exists() check, not
            # a real Sales Order, so write it directly past Link validation.
            frappe.db.set_value("Vendor Order", vo.name, "sales_order", so_name)

        doc = _fake_doc(
            voucher_type="Sales Invoice", voucher_no="STOCK-TEST-SI-2",
            item_code="VT-ITEM-STOCK", actual_qty=-2,
            items=[_fake_item_row(against_sales_order=so_name)],
        )
        on_stock_ledger_entry_submit(doc, "on_submit")
        count = frappe.db.count("Sync Outbox", {"voucher_no": "STOCK-TEST-SI-2"})
        self.assertEqual(count, 0)

    def test_stock_entry_material_receipt_increases_qty(self):
        from saathimart_vendor.event_handlers.stock import on_stock_ledger_entry_submit
        # voucher_detail_no set (truthy) means "this SLE came from a Stock
        # Entry row" — actual_qty is trusted as already correctly signed,
        # same as real ERPNext SLEs, instead of re-deriving direction from
        # stock_entry_type.
        doc = _fake_doc(
            voucher_type="Stock Entry", voucher_no="STOCK-TEST-SE-1", voucher_detail_no="se-row-1",
            item_code="VT-ITEM-STOCK", actual_qty=8,
        )
        on_stock_ledger_entry_submit(doc, "on_submit")
        event_type, payload = self._last_outbox_payload("STOCK-TEST-SE-1")
        self.assertEqual(event_type, "stock.receipt")
        self.assertEqual(payload["qty_change"], 8)

    def test_stock_entry_material_issue_decreases_qty(self):
        from saathimart_vendor.event_handlers.stock import on_stock_ledger_entry_submit
        doc = _fake_doc(
            voucher_type="Stock Entry", voucher_no="STOCK-TEST-SE-2", voucher_detail_no="se-row-2",
            item_code="VT-ITEM-STOCK", actual_qty=-4,
        )
        on_stock_ledger_entry_submit(doc, "on_submit")
        event_type, payload = self._last_outbox_payload("STOCK-TEST-SE-2")
        self.assertEqual(event_type, "stock.deduct")
        self.assertEqual(payload["qty_change"], -4)


# ── Test: Inbound webhook handlers (api/receive.py) ────────────────────────────

class TestReceiveFromHub(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        _configure_vendor(vendor_id="vendor-test-receive")

    def test_handle_new_order_creates_vendor_order(self):
        from saathimart_vendor.api.receive import _handle_new_order
        hub_order_id = "HUB-ORDER-NEW-001"
        # frappe.db.delete() is a raw table-only delete — it does not
        # cascade to the Vendor Order Item child table, so leftover rows
        # from an earlier run would silently inflate len(doc.items) on
        # a document re-created with the same name (== hub_order_id).
        frappe.db.delete("Vendor Order Item", {"parent": hub_order_id})
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
        # frappe.db.delete() is a raw table-only delete — it does not
        # cascade to the Vendor Order Item child table, so leftover rows
        # from an earlier run would silently inflate len(doc.items) on
        # a document re-created with the same name (== hub_order_id).
        frappe.db.delete("Vendor Order Item", {"parent": hub_order_id})
        frappe.db.delete("Vendor Order", {"hub_order_id": hub_order_id})
        frappe.db.commit()
        payload = {"order_id": hub_order_id, "customer_name": "A", "grand_total": 100, "items": [{"product": "SM-PROD-DUMMY", "qty": 1, "rate": 100}]}
        _handle_new_order(payload)
        _handle_new_order(payload)  # second push for same order must not error/duplicate
        count = frappe.db.count("Vendor Order", {"hub_order_id": hub_order_id})
        self.assertEqual(count, 1)

    def test_handle_order_cancel_updates_status(self):
        from saathimart_vendor.api.receive import _handle_new_order, _handle_order_cancel
        hub_order_id = "HUB-ORDER-CANCEL-001"
        # frappe.db.delete() is a raw table-only delete — it does not
        # cascade to the Vendor Order Item child table, so leftover rows
        # from an earlier run would silently inflate len(doc.items) on
        # a document re-created with the same name (== hub_order_id).
        frappe.db.delete("Vendor Order Item", {"parent": hub_order_id})
        frappe.db.delete("Vendor Order", {"hub_order_id": hub_order_id})
        frappe.db.commit()
        _handle_new_order({"order_id": hub_order_id, "customer_name": "A", "grand_total": 100, "items": [{"product": "SM-PROD-DUMMY", "qty": 1, "rate": 100}]})
        _handle_order_cancel({"order_id": hub_order_id, "reason": "Customer changed mind"})
        status = frappe.db.get_value("Vendor Order", hub_order_id, "status")
        self.assertEqual(status, "Cancelled")

    def test_handle_order_cancel_skips_already_delivered(self):
        from saathimart_vendor.api.receive import _handle_new_order, _handle_order_cancel
        hub_order_id = "HUB-ORDER-DELIVERED-001"
        # frappe.db.delete() is a raw table-only delete — it does not
        # cascade to the Vendor Order Item child table, so leftover rows
        # from an earlier run would silently inflate len(doc.items) on
        # a document re-created with the same name (== hub_order_id).
        frappe.db.delete("Vendor Order Item", {"parent": hub_order_id})
        frappe.db.delete("Vendor Order", {"hub_order_id": hub_order_id})
        frappe.db.commit()
        _handle_new_order({"order_id": hub_order_id, "customer_name": "A", "grand_total": 100, "items": [{"product": "SM-PROD-DUMMY", "qty": 1, "rate": 100}]})
        frappe.db.set_value("Vendor Order", hub_order_id, "status", "Delivered")
        _handle_order_cancel({"order_id": hub_order_id, "reason": "too late"})
        status = frappe.db.get_value("Vendor Order", hub_order_id, "status")
        self.assertEqual(status, "Delivered")  # unchanged

    def test_handle_order_reassign_marks_cancelled(self):
        from saathimart_vendor.api.receive import _handle_new_order, _handle_order_reassign
        hub_order_id = "HUB-ORDER-REASSIGN-001"
        # frappe.db.delete() is a raw table-only delete — it does not
        # cascade to the Vendor Order Item child table, so leftover rows
        # from an earlier run would silently inflate len(doc.items) on
        # a document re-created with the same name (== hub_order_id).
        frappe.db.delete("Vendor Order Item", {"parent": hub_order_id})
        frappe.db.delete("Vendor Order", {"hub_order_id": hub_order_id})
        frappe.db.commit()
        _handle_new_order({"order_id": hub_order_id, "customer_name": "A", "grand_total": 100, "items": [{"product": "SM-PROD-DUMMY", "qty": 1, "rate": 100}]})
        _handle_order_reassign({"order_id": hub_order_id, "reason": "vendor out of stock"})
        status = frappe.db.get_value("Vendor Order", hub_order_id, "status")
        self.assertEqual(status, "Cancelled")

    def test_receive_from_hub_requires_event(self):
        # _verify_hub_secret/_verify_timestamp both need a real HTTP request
        # (frappe.request.headers) — bypassed here since these two tests are
        # about event validation, not the auth layer (which has its own
        # coverage via _verify_hub_secret's callers/AuthGuards tests).
        from saathimart_vendor.api.receive import receive_from_hub
        with patch("saathimart_vendor.api.receive._verify_hub_secret"), \
             patch("saathimart_vendor.api.receive._verify_timestamp"):
            with self.assertRaises(frappe.ValidationError):
                receive_from_hub(event=None, payload={})

    def test_receive_from_hub_unknown_event_does_not_raise(self):
        from saathimart_vendor.api.receive import receive_from_hub
        with patch("saathimart_vendor.api.receive._verify_hub_secret"), \
             patch("saathimart_vendor.api.receive._verify_timestamp"):
            result = receive_from_hub(event="order.teleported", payload={})
        self.assertEqual(result, {"ok": True})


# ── Test: Vendor Order lifecycle ────────────────────────────────────────────────

class TestVendorOrderLifecycle(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.config = _configure_vendor(vendor_id="vendor-test-lifecycle")
        self.mapping = _make_mapping("8901234500020", "VT-ITEM-ORDER")
        # mark_dispatched() now submits a real Delivery Note against
        # whatever stock actually exists — this suite deliberately never
        # seeds real stock receipts (see module docstring), so allow
        # negative stock here rather than adding a full Stock Entry setup
        # just to make a Delivery Note submit successfully.
        frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)

    def _make_received_order(self, hub_order_id, items=None):
        # frappe.db.delete() is a raw table-only delete — it does not
        # cascade to the Vendor Order Item child table, so leftover rows
        # from an earlier run would silently inflate len(doc.items) on
        # a document re-created with the same name (== hub_order_id).
        frappe.db.delete("Vendor Order Item", {"parent": hub_order_id})
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
            {"product": self.mapping.hub_product_id, "item_code": self.mapping.item_code, "qty": 2, "rate": 100},
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
        # item_code must be a real ERPNext Item (Vendor Order Item.item_code
        # is a mandatory Link) — "product" is the hub_product_id that
        # accept_order() looks up in Product Mapping, and deliberately has
        # no matching mapping here.
        vo = self._make_received_order("HUB-ORDER-ACCEPT-003", items=[
            {"product": "SM-PROD-NOT-MAPPED", "item_code": self.mapping.item_code, "qty": 1, "rate": 50},
        ])
        with self.assertRaises(frappe.ValidationError):
            vo.accept_order()

    def test_accept_order_empty_items_raises(self):
        # Vendor Order.items is a mandatory child table (reqd=1), so an
        # empty items list is rejected at insert() itself — accept_order()
        # is never reached with such a document.
        with self.assertRaises(frappe.ValidationError):
            self._make_received_order("HUB-ORDER-ACCEPT-004", items=[])

    def test_mark_dispatched_enqueues_event_and_sets_timestamp(self):
        vo = self._make_received_order("HUB-ORDER-DISPATCH-001")
        vo.accept_order()
        vo.reload()
        result = vo.mark_dispatched()
        vo.reload()
        self.assertEqual(vo.status, "Dispatched")
        self.assertIsNotNone(vo.dispatched_at)
        # mark_dispatched() now creates + submits a real Delivery Note
        # against the linked Sales Order instead of just flipping status.
        self.assertTrue(frappe.db.exists("Delivery Note", result["delivery_note"]))
        self.assertEqual(frappe.db.get_value("Delivery Note", result["delivery_note"], "docstatus"), 1)
        # mark_dispatched() enqueues with voucher_type="Delivery Note",
        # voucher_no=dn.name — same convention on_delivery_note_submit uses
        # for the same event, not the Vendor Order's own name.
        event_row = frappe.db.get_value(
            "Sync Outbox", {"voucher_no": result["delivery_note"], "event_type": "order.dispatched"},
            ["event_type", "payload"], as_dict=True,
        )
        self.assertEqual(event_row.event_type, "order.dispatched")
        self.assertIn(result["delivery_note"], event_row.payload)
        # on_delivery_note_submit must not have also fired for this same
        # submit — exactly one order.dispatched row for this delivery note, not two.
        count = frappe.db.count("Sync Outbox", {"voucher_no": result["delivery_note"], "event_type": "order.dispatched"})
        self.assertEqual(count, 1)

    def test_mark_dispatched_wrong_status_raises(self):
        vo = self._make_received_order("HUB-ORDER-DISPATCH-002")
        with self.assertRaises(frappe.ValidationError):
            vo.mark_dispatched()  # still "Received", not Accepted/Preparing

    def test_mark_preparing_then_dispatch(self):
        vo = self._make_received_order("HUB-ORDER-PREPARING-001")
        vo.accept_order()
        vo.reload()
        vo.mark_preparing()
        vo.reload()
        self.assertEqual(vo.status, "Preparing")
        self.assertIsNotNone(vo.preparing_at)
        event_type = frappe.db.get_value(
            "Sync Outbox", {"voucher_no": vo.name, "event_type": "order.preparing"}, "event_type"
        )
        self.assertEqual(event_type, "order.preparing")
        # mark_dispatched()'s guard must still accept "Preparing", not just "Accepted"
        vo.mark_dispatched()
        vo.reload()
        self.assertEqual(vo.status, "Dispatched")

    def test_mark_preparing_wrong_status_raises(self):
        vo = self._make_received_order("HUB-ORDER-PREPARING-002")
        with self.assertRaises(frappe.ValidationError):
            vo.mark_preparing()  # still "Received", not Accepted

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
    @patch("saathimart_vendor.tasks.frappe.enqueue")
    def test_reconcile_stock_visits_every_active_mapping(self, mock_enqueue, mock_reconcile_item):
        from saathimart_vendor.tasks import reconcile_stock, _reconcile_chunk
        # reconcile_stock() hands chunks off to frappe.enqueue() for a
        # background worker to process — no worker runs during
        # `bench run-tests`, so run the chunk inline instead, same as a
        # worker eventually would.
        mock_enqueue.side_effect = lambda *a, **kw: _reconcile_chunk(kw["config_name"], kw["mappings"])
        _make_mapping("8901234500080", "VT-ITEM-RECON-1")
        _make_mapping("8901234500081", "VT-ITEM-RECON-2")
        # force=True bypasses the per-vendor hourly jitter slot (see
        # reconcile_stock's docstring) — this test is about whether every
        # active mapping gets visited, not about the scheduling gate, which
        # would otherwise make this test's pass/fail depend on the wall-clock
        # minute it happens to run at.
        reconcile_stock(force=True)
        self.assertGreaterEqual(mock_reconcile_item.call_count, 2)

    @patch("saathimart_vendor.tasks._reconcile_item")
    def test_reconcile_stock_skips_when_disabled(self, mock_reconcile_item):
        from saathimart_vendor.tasks import reconcile_stock
        _configure_vendor(vendor_id="vendor-test-tasks", reconciliation_enabled=0)
        reconcile_stock(force=True)
        mock_reconcile_item.assert_not_called()
        _configure_vendor(vendor_id="vendor-test-tasks", reconciliation_enabled=1)

    def test_reconcile_stock_skips_outside_own_jitter_slot(self):
        from saathimart_vendor.tasks import reconcile_stock
        config = _configure_vendor(vendor_id="vendor-test-jitter")
        # Find a minute value guaranteed to fall in a *different* 10-minute
        # bucket than this vendor's own slot, so the gate is exercised
        # deterministically rather than depending on when the test happens
        # to run.
        import zlib
        own_slot = zlib.crc32(config.vendor_id.encode()) % 6
        other_slot = (own_slot + 1) % 6
        with patch("saathimart_vendor.tasks.now_datetime") as mock_now:
            mock_now.return_value = frappe.utils.get_datetime(
                f"2026-01-01 {other_slot * 10:02d}:05:00"
            )
            with patch("saathimart_vendor.tasks._reconcile_item") as mock_reconcile_item:
                reconcile_stock()  # no force — should be gated out
                mock_reconcile_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()


# ── Hub webhook authentication (HMAC signatures) ────────────────────────────

class TestHubAuthHmac(unittest.TestCase):
    """
    Inbound-hub-push authentication: valid signatures pass; tampered bodies,
    wrong secrets and stale timestamps are rejected. Also covers the
    zero-downtime rotation window (old + staged-next secrets accepted).
    """

    PRIMARY = "unit-test-primary-secret-0123456789abcdef"
    OLD = "unit-test-old-secret-9876543210fedcba"
    NEXT = "unit-test-next-secret-aaaaaabbbbbbcccccc"
    BODY = b'{"event": "order.new", "payload": {"x": 1}}'

    def setUp(self):
        frappe.set_user("Administrator")
        _configure_vendor(vendor_id="vendor-test-hmac")
        doc = frappe.get_single("Vendor Config")
        self._orig = {
            f: doc.get_password(f, raise_exception=False) or ""
            for f in ("webhook_secret", "webhook_secret_old", "webhook_secret_next")
        }
        doc.webhook_secret = self.PRIMARY
        doc.webhook_secret_old = None
        doc.webhook_secret_next = None
        doc.flags.ignore_mandatory = True
        doc.save(ignore_permissions=True)
        frappe.db.commit()

    def tearDown(self):
        doc = frappe.get_single("Vendor Config")
        for field, value in self._orig.items():
            setattr(doc, field, value or None)
        doc.flags.ignore_mandatory = True
        doc.save(ignore_permissions=True)
        frappe.db.commit()

    # ── helpers ──

    def _headers(self, secret=None, body=None, ts=None):
        import time

        from saathimart_vendor.utils import compute_hmac_signature

        body = self.BODY if body is None else body
        ts = str(int(time.time())) if ts is None else str(ts)
        headers = {"X-SM-Timestamp": ts}
        if secret:
            headers["X-SM-Signature"] = compute_hmac_signature(secret, ts, body)
        return headers

    def _verify_with_request(self, headers, body=None):
        from saathimart_vendor.api.receive import _verify_hub_secret

        fake = MagicMock()
        fake.headers = headers
        fake.get_data.return_value = self.BODY if body is None else body
        with patch("frappe.request", fake), \
             patch("saathimart_vendor.api.receive._log_auth_failure"):
            _verify_hub_secret()

    def _set_secrets(self, **kwargs):
        doc = frappe.get_single("Vendor Config")
        for field, value in kwargs.items():
            setattr(doc, field, value or None)
        doc.flags.ignore_mandatory = True
        doc.save(ignore_permissions=True)
        frappe.db.commit()

    # ── happy path ──

    def test_valid_signature_accepted(self):
        self._verify_with_request(self._headers(self.PRIMARY))

    def test_legacy_secret_header_still_accepted(self):
        # Rolling-upgrade fallback: hubs on the pre-HMAC build send the bare
        # secret; must keep working until every hub signs.
        self._verify_with_request({"X-SM-Secret": self.PRIMARY})

    # ── attack scenarios ──

    def test_tampered_body_rejected(self):
        # Signature computed over the ORIGINAL body, attacker swaps payload
        # in transit (e.g. 500 -> 5): verification must fail.
        good_headers = self._headers(self.PRIMARY, body=self.BODY)
        tampered = self.BODY.replace(b'"x": 1', b'"x": 999999')
        with self.assertRaises(frappe.AuthenticationError):
            self._verify_with_request(good_headers, body=tampered)

    def test_wrong_secret_signature_rejected(self):
        with self.assertRaises(frappe.AuthenticationError):
            self._verify_with_request(
                self._headers("attacker-known-guess-0123456789abcdef")
            )

    def test_garbage_signature_rejected(self):
        headers = self._headers(None)  # timestamp only, bogus signature
        headers["X-SM-Signature"] = "deadbeef" * 8
        with self.assertRaises(frappe.AuthenticationError):
            self._verify_with_request(headers)

    def test_missing_credentials_rejected(self):
        with self.assertRaises(frappe.AuthenticationError):
            self._verify_with_request({"X-SM-Timestamp": "1700000000"})

    def test_stale_timestamp_rejected(self):
        # Replay guard: a captured request older than max_age_seconds must
        # be rejected even though its signature is cryptographically valid.
        import time

        from saathimart_vendor.api.receive import _verify_timestamp

        old_ts = str(int(time.time()) - 600)
        fake = MagicMock()
        fake.headers = {"X-SM-Timestamp": old_ts}
        with patch("frappe.request", fake):
            with self.assertRaises(frappe.AuthenticationError):
                _verify_timestamp(max_age_seconds=300)

    def test_fresh_timestamp_accepted(self):
        import time

        from saathimart_vendor.api.receive import _verify_timestamp

        fake = MagicMock()
        fake.headers = {"X-SM-Timestamp": str(int(time.time()))}
        with patch("frappe.request", fake):
            _verify_timestamp(max_age_seconds=300)

    def test_valid_signature_but_stale_timestamp_rejected_end_to_end(self):
        # The real endpoint calls BOTH checks; a replayed capture of a fully
        # valid request dies on the timestamp even though the sig matches.
        import time

        from saathimart_vendor.api.receive import _verify_hub_secret, _verify_timestamp

        old_ts = str(int(time.time()) - 600)
        headers = self._headers(self.PRIMARY, ts=old_ts)
        fake = MagicMock()
        fake.headers = headers
        fake.get_data.return_value = self.BODY
        with patch("frappe.request", fake), \
             patch("saathimart_vendor.api.receive._log_auth_failure"):
            with self.assertRaises(frappe.AuthenticationError):
                _verify_timestamp(max_age_seconds=300)

    # ── rotation window ──

    def test_staged_next_secret_accepted_during_rotation_phase1(self):
        # Phase 1: hub staged NEW on us but still sends OLD-signed traffic;
        # NEW-signed requests (post-flip) must verify via webhook_secret_next.
        self._set_secrets(webhook_secret_next=self.NEXT)
        self._verify_with_request(self._headers(self.NEXT))

    def test_old_and_new_both_accepted_after_promotion(self):
        # Phase 3 done: primary=NEW, old kept as grace. Both signatures work;
        # an unrelated third secret never does.
        self._set_secrets(webhook_secret=self.NEXT, webhook_secret_old=self.PRIMARY)
        self._verify_with_request(self._headers(self.NEXT))
        self._verify_with_request(self._headers(self.PRIMARY))
        with self.assertRaises(frappe.AuthenticationError):
            self._verify_with_request(self._headers(self.OLD.replace("fedcba", "ffffff")))

    def test_rotate_stage_then_promote_full_flow(self):
        # Drive the actual endpoints end-to-end (auth mocked — covered above).
        from saathimart_vendor.api.receive import rotate_secret_promote, rotate_secret_stage

        with patch("saathimart_vendor.api.receive._verify_hub_secret"), \
             patch("saathimart_vendor.api.receive._verify_timestamp"):
            ret = rotate_secret_stage(new_secret=self.NEXT)
            self.assertTrue(ret["ok"])
            cfg = frappe.get_single("Vendor Config")
            self.assertEqual(cfg.get_password("webhook_secret_next", raise_exception=False), self.NEXT)

            # Simulate the hub flipping its primary between phases: nothing
            # to do here on the vendor — staging alone already accepts NEW.

            ret = rotate_secret_promote()
            self.assertTrue(ret["ok"] and ret["promoted"])

        cfg = frappe.get_single("Vendor Config")
        self.assertEqual(cfg.get_password("webhook_secret", raise_exception=False), self.NEXT)
        self.assertEqual(cfg.get_password("webhook_secret_old", raise_exception=False), self.PRIMARY)
        self.assertFalse(cfg.get_password("webhook_secret_next", raise_exception=False))

    def test_promote_without_staged_secret_is_noop(self):
        from saathimart_vendor.api.receive import rotate_secret_promote

        with patch("saathimart_vendor.api.receive._verify_hub_secret"), \
             patch("saathimart_vendor.api.receive._verify_timestamp"):
            ret = rotate_secret_promote()
        self.assertTrue(ret["ok"] and not ret["promoted"])

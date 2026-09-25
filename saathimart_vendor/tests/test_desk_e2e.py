"""
Desk e2e for the vendor side, as a repo test.

The manual pre-release desk pass on a vendor site included three accounting
checks that no unit test covered, because they only exist in real ERPNext
flows:

1. Stock Entry (Material Receipt) — submit moves real stock: Stock Ledger
   Entry rows exist, the Bin's actual_qty moved, and (with stock accounting
   active) the GL rows balance to the entry value.
2. Journal Entry — submits cleanly and its GL rows balance (the desk's
   "GL balanced" eyeball check, automated).
3. The hub's platform.ledger_entry batch (commission income / vendor
   clearing / VAT for a paid order) books a real, submitted Journal Entry
   whose GL rows balance — the vendor-side half of the hub's paid-order
   ledger chain. This is the desk-facing evidence for COD orders paid on
   delivery: the batch the hub enqueues must end up as bookable entries.

Run (inside the vendors container):
    bench --site vendor1.localhost run-tests --module saathimart_vendor.tests.test_desk_e2e
"""
import unittest
from unittest import mock

import frappe
from frappe.utils import flt, nowdate


# ── Fixture helpers (same conventions as test_saathimart_vendor) ─────────────

def _ensure_fiscal_year():
    today = nowdate()
    if frappe.db.exists("Fiscal Year", {"year_start_date": ["<=", today],
                                        "year_end_date": [">=", today]}):
        return
    company = _ensure_company()
    doc = frappe.new_doc("Fiscal Year")
    doc.year = f"DeskE2E {today[:4]}"
    doc.year_start_date = f"{today[:4]}-01-01"
    doc.year_end_date = f"{today[:4]}-12-31"
    doc.append("fiscal_year_companies", {"company": company})
    doc.insert(ignore_permissions=True)
    frappe.db.commit()


def _ensure_base_fixtures():
    # Item Group tree root + UOM + Price Lists (bare test DBs lack them)
    if not frappe.db.exists("Item Group", "All Item Groups"):
        frappe.get_doc({"doctype": "Item Group", "item_group_name": "All Item Groups",
                        "is_group": 1}).insert(ignore_permissions=True)
    for uom, enabled in [("Nos", 1), ("Unit", 1)]:
        if not frappe.db.exists("UOM", uom):
            frappe.get_doc({"doctype": "UOM", "uom_name": uom, "enabled": enabled})\
                .insert(ignore_permissions=True)
    for pl_name, buying, selling in [("Standard Buying", 1, 0), ("Standard Selling", 0, 1)]:
        if not frappe.db.exists("Price List", pl_name):
            frappe.get_doc({
                "doctype": "Price List", "price_list_name": pl_name, "enabled": 1,
                "buying": buying, "selling": selling, "currency": "NPR",
            }).insert(ignore_permissions=True)
    frappe.db.commit()


def _ensure_company(name="Vendor Test Co", abbr="VTC"):
    _ensure_base_fixtures()
    if frappe.db.exists("Company", name):
        return name
    doc = frappe.new_doc("Company")
    doc.company_name = name
    doc.abbr = abbr
    doc.default_currency = "NPR"
    doc.country = "Nepal"
    doc.insert(ignore_permissions=True)
    frappe.db.set_default("company", doc.name)
    frappe.db.commit()
    return doc.name


def _ensure_warehouse(company=None):
    company = company or _ensure_company()
    abbr = frappe.db.get_value("Company", company, "abbr")
    wh_name = f"Stores - {abbr}"
    if frappe.db.exists("Warehouse", wh_name):
        return wh_name
    doc = frappe.new_doc("Warehouse")
    doc.warehouse_name = "Vendor Test Store"
    doc.company = company
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


def _make_item(item_code, valuation_rate=100):
    _ensure_base_fixtures()
    if frappe.db.exists("Item", item_code):
        return frappe.get_doc("Item", item_code)
    doc = frappe.new_doc("Item")
    doc.item_code = item_code
    doc.item_name = item_code
    doc.item_group = "All Item Groups"
    doc.stock_uom = "Nos"
    doc.is_stock_item = 1
    doc.insert(ignore_permissions=True)
    return doc


def _stock_account(company=None):
    """A leaf stock account for GL-balance assertions; None → skip GL checks."""
    company = company or _ensure_company()
    abbr = frappe.db.get_value("Company", company, "abbr")
    for candidate in (f"Stock In Hand - {abbr}", f"Finished Goods - {abbr}",
                      f"Stores - {abbr}", f"Stock - {abbr}"):
        if frappe.db.exists("Account", candidate):
            return candidate
    return frappe.db.get_value(
        "Account", {"company": company, "is_group": 0, "root_type": "Asset"}, "name"
    )


class TestStockEntrySubmitCancel(unittest.TestCase):
    """Material Receipt Stock Entry: real stock movement + balanced GL."""

    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        _ensure_fiscal_year()
        cls.company = _ensure_company()
        cls.warehouse = _ensure_warehouse(cls.company)
        cls.item = _make_item("DESK-E2E-ITEM").name

    def _make_stock_entry(self, qty=10, rate=100):
        se = frappe.new_doc("Stock Entry")
        se.stock_entry_type = "Material Receipt"
        se.company = self.company
        se.append("items", {
            "item_code": self.item,
            "qty": qty,
            "basic_rate": rate,
            "t_warehouse": self.warehouse,
        })
        se.insert(ignore_permissions=True)
        se.submit()
        return se

    def test_submit_moves_real_stock(self):
        bin_filters = {"item_code": self.item, "warehouse": self.warehouse}
        # Baseline: sibling tests share this committed Bin (FrappeTestCase
        # rolls back rows we insert, but submitted SEs commit), so assert
        # delta vs before, not absolute zero.
        before = flt(frappe.db.get_value("Bin", bin_filters, "actual_qty") or 0)
        se = self._make_stock_entry(qty=7, rate=100)

        self.assertEqual(se.docstatus, 1)
        sle = frappe.get_all("Stock Ledger Entry",
                             filters={"voucher_no": se.name},
                             fields=["actual_qty", "warehouse"])
        self.assertEqual(len(sle), 1)
        self.assertEqual(flt(sle[0]["actual_qty"]), 7)
        self.assertEqual(sle[0]["warehouse"], self.warehouse)

        bin_qty = frappe.db.get_value("Bin", bin_filters, "actual_qty")
        self.assertEqual(flt(bin_qty), before + 7)

        # cancel → this voucher's stock movement fully reverses
        se.cancel()
        bin_qty = frappe.db.get_value("Bin", bin_filters, "actual_qty")
        self.assertEqual(flt(bin_qty), before)

    def test_submitted_stock_entry_gl_balances(self):
        """The desk's accounting check for stock moves: GL must balance."""
        se = self._make_stock_entry(qty=3, rate=100)

        gl = frappe.get_all("GL Entry",
                            filters={"voucher_type": "Stock Entry", "voucher_no": se.name},
                            fields=["account", "debit", "credit"])
        if not gl:
            self.skipTest("no GL rows for this Stock Entry (stock accounting not active)")
        total_dr = round(sum(flt(r.debit) for r in gl), 2)
        total_cr = round(sum(flt(r.credit) for r in gl), 2)
        self.assertEqual(total_dr, total_cr)

    def test_cancelled_entry_reverses_gl(self):
        se = self._make_stock_entry(qty=2, rate=50)
        se.cancel()

        gl = frappe.get_all("GL Entry",
                            filters={"voucher_type": "Stock Entry", "voucher_no": se.name},
                            fields=["is_cancelled", "debit", "credit"])
        if not gl:
            self.skipTest("no GL rows for this Stock Entry (stock accounting not active)")
        # v15+: cancellation rows carry is_cancelled=1 — the ledger must show
        # the reversal, not silently delete the original posting.
        self.assertTrue(any(flt(r.is_cancelled) == 1 for r in gl))


class TestJournalEntryBalance(unittest.TestCase):
    """A manually-booked JE submits and its GL rows balance."""

    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        _ensure_fiscal_year()
        cls.company = _ensure_company()
        cls.cash = frappe.db.get_value(
            "Account", {"company": cls.company, "account_name": "Cash", "is_group": 0}, "name")
        cls.expense = frappe.db.get_value(
            "Account", {"company": cls.company,
                        "account_name": "Commission on Sales", "is_group": 0}, "name")
        if not (cls.cash and cls.expense):
            raise unittest.SkipTest("Cash / Commission on Sales accounts missing")

    def test_je_submits_and_gl_balances(self):
        je = frappe.new_doc("Journal Entry")
        je.entry_type = "Journal Entry"
        je.company = self.company
        je.posting_date = nowdate()
        je.cheque_no = "DESK-E2E-JE"
        je.cheque_date = nowdate()
        je.user_remark = "desk e2e balance probe"
        je.append("accounts", {"account": self.cash, "debit_in_account_currency": 500,
                               "credit_in_account_currency": 0})
        je.append("accounts", {"account": self.expense, "debit_in_account_currency": 0,
                               "credit_in_account_currency": 500})
        je.insert(ignore_permissions=True)
        je.submit()

        self.assertEqual(je.docstatus, 1)
        gl = frappe.get_all("GL Entry",
                            filters={"voucher_type": "Journal Entry", "voucher_no": je.name},
                            fields=["account", "debit", "credit"])
        self.assertGreaterEqual(len(gl), 2)
        self.assertEqual(
            round(sum(flt(r.debit) for r in gl), 2),
            round(sum(flt(r.credit) for r in gl), 2),
        )


class TestPlatformLedgerReceiver(unittest.TestCase):
    """
    Vendor-side half of the paid-order ledger chain: the hub pushes a
    platform.ledger_entry batch (commission income, vendor clearing, VAT)
    for a paid COD order; it must book a real submitted Journal Entry,
    balanced, with sm_hub_ref idempotency.
    """

    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        _ensure_fiscal_year()
        cls.company = _ensure_company()

    def _payload(self, order_id, amount=2000, commission=200):
        """The same batch shape the hub's accounting.py computes and pushes."""
        vat = round(commission * 0.13, 2)
        clearing = round(amount - commission - vat, 2)
        return {
            "voucher_type": "Payment Entry",
            "voucher_no": order_id,
            "remarks": f"Payment received for order {order_id} via COD",
            "event_id": f"platform.ledger_entry.Payment Entry.{order_id}.payment",
            "entries": [
                {"account_key": "cash_bank", "debit": amount, "credit": 0,
                 "remarks": f"Cash in for {order_id}"},
                {"account_key": "commission_income", "debit": 0, "credit": commission,
                 "remarks": f"Commission for {order_id}"},
                {"account_key": "vat_output", "debit": 0, "credit": vat,
                 "remarks": f"Service VAT on commission for {order_id}"},
                {"account_key": "clearing_vendor", "debit": 0, "credit": clearing,
                 "remarks": f"Vendor clearing for {order_id}"},
            ],
        }

    def test_batch_books_submitted_balanced_je(self):
        from saathimart_vendor.api.vendor_accounting import create_platform_gl_entries

        order_id = f"DESK-E2E-ORDER-{frappe.generate_hash(length=6)}"
        payload = self._payload(order_id, amount=2000, commission=200)

        result = create_platform_gl_entries(payload)
        self.assertTrue(result.get("ok"), f"receiver rejected the batch: {result}")
        je_name = result["journal_entry"]
        je = frappe.get_doc("Journal Entry", je_name)
        self.assertEqual(je.docstatus, 1)          # visible in the desk list
        self.assertEqual(je.sm_hub_ref, payload["event_id"])

        gl = frappe.get_all("GL Entry",
                            filters={"voucher_type": "Journal Entry", "voucher_no": je_name},
                            fields=["account", "debit", "credit"])
        self.assertGreaterEqual(len(gl), 4)
        total_dr = round(sum(flt(r.debit) for r in gl), 2)
        total_cr = round(sum(flt(r.credit) for r in gl), 2)
        self.assertEqual(total_dr, total_cr)
        self.assertEqual(total_dr, 2000.0)  # cash in equals the whole batch

    def test_batch_replay_is_idempotent(self):
        from saathimart_vendor.api.vendor_accounting import create_platform_gl_entries

        order_id = f"DESK-E2E-ORDER-{frappe.generate_hash(length=6)}"
        payload = self._payload(order_id, amount=1500, commission=150)

        first = create_platform_gl_entries(payload)
        self.assertTrue(first.get("ok"))
        second = create_platform_gl_entries(payload)
        self.assertTrue(second.get("duplicate"),
                        f"replay must not double-book: {second}")
        self.assertEqual(second.get("entries"), 0)

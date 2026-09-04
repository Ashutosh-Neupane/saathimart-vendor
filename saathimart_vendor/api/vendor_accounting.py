"""
Vendor-Side Accounting Engine for SaathiMart-Vendor

Handles all accounting from the vendor/franchise perspective:
  - Sales Invoice creation (to customer, via platform clearing)
  - Settlement Journal Entry (when platform pays vendor — the only cash event)
  - Clearing Account management with SaathiMart Platform
  - VAT tracking on product sales
  - Commission expense tracking
  - Platform coupon/loyalty reimbursement tracking

Three-party clearing house model:
  The vendor never receives cash from the customer directly.
  The platform collects payment and settles with the vendor periodically.
  The vendor's receivable sits in "SaathiMart Clearing Account" until
  the platform settles (creates a Journal Entry that debits Bank and
  credits the Clearing Account).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, nowdate


# ── Vendor Chart of Accounts ─────────────────────────────────────────────────
# These accounts MUST exist in the vendor's ERPNext Chart of Accounts.

VENDOR_ACCOUNTS = {
    "cash_bank":              "Cash/Bank",
    "revenue":                "Sales",
    "vat_output":             "Output VAT",
    "vat_input":              "Input VAT",
    "clearing_platform":      "SaathiMart Clearing",
    "commission_expense":     "Marketplace Commission",
    "platform_coupon_income": "Platform Coupon Reimbursement",
    "loyalty_income":         "Loyalty Reimbursement",
    "accounts_receivable":    "Accounts Receivable",
    "accounts_payable":       "Accounts Payable",
}

# ERPNext appends " - XX" (company abbreviation) to account names.
# We search by LIKE to match regardless of the suffix.
_ACCOUNT_NAME_MAP = {
    "cash_bank":              "Cash/Bank",
    "revenue":                "Sales",
    "vat_output":             "VAT",
    "vat_input":              "VAT",
    "clearing_platform":      "SaathiMart Clearing",
    "commission_expense":     "Marketplace Commission",
    "platform_coupon_income": "Platform Coupon Reimbursement",
    "loyalty_income":         "Loyalty Reimbursement",
    "accounts_receivable":    "Accounts Receivable",
    "accounts_payable":       "Accounts Payable",
}


# Fuzzy fallback: if the exact name doesn't exist, search by keyword
_FUZZY_FALLBACKS = {
    "cash_bank":              ["Cash In Hand", "Cash", "Bank"],
    "revenue":                ["Sales"],
    "vat_output":             ["VAT", "Duties and Taxes"],
    "vat_input":              ["VAT", "Duties and Taxes"],
    "clearing_platform":      ["Accounts Receivable", "Debtors"],
    "commission_expense":     ["Commission on Sales", "Indirect Expenses"],
    "platform_coupon_income": ["Indirect Income"],
    "loyalty_income":         ["Indirect Income"],
    "accounts_receivable":    ["Accounts Receivable", "Debtors"],
    "accounts_payable":       ["Accounts Payable", "Creditors"],
}


def _get_account(account_key):
    """Get account name from chart of accounts.

    ERPNext appends " - XX" (company abbreviation) to account names, so
    we search by LIKE to match regardless of suffix. GL entries require
    leaf (non-group) accounts.
    """
    # Check cached value first
    cached = VENDOR_ACCOUNTS.get(account_key)
    if cached and frappe.db.exists("Account", cached):
        return cached

    # Search by the mapped name (handles company suffix)
    search_name = _ACCOUNT_NAME_MAP.get(account_key, VENDOR_ACCOUNTS.get(account_key, ""))
    if not search_name:
        frappe.log_error(f"Vendor account {account_key} not found", "Vendor Accounting")
        return None

    # Prefer leaf accounts
    found = frappe.db.get_value(
        "Account",
        {"account_name": ["like", f"{search_name}%"], "is_group": 0},
        "name",
    )
    if found:
        VENDOR_ACCOUNTS[account_key] = found
        return found

    # Fuzzy fallback — prefer non-group
    for keyword in _FUZZY_FALLBACKS.get(account_key, []):
        found = frappe.db.get_value(
            "Account",
            {"account_name": ["like", f"%{keyword}%"], "is_group": 0},
            "name",
        )
        if found:
            VENDOR_ACCOUNTS[account_key] = found
            return found

    frappe.log_error(f"Vendor account {account_key} does not exist", "Vendor Accounting")
    return None


def _get_company():
    """Get the default company for this vendor site."""
    company = frappe.defaults.get_global_default("company")
    if not company:
        company = frappe.db.get_value("Company", {}, "name")
    return company


def _get_default_cost_center(company):
    """Get the default cost center for the company."""
    cc = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
    if not cc:
        cc = frappe.db.get_value("Cost Center", {"is_group": 0}, "name")
    return cc


def create_gl_entry(account, debit=0, credit=0, voucher_type="Payment Entry",
                    voucher_no="", remarks="", party_type=None, party=None,
                    posting_date=None, cost_center=None):
    """Create a single GL Entry."""
    company = _get_company()
    if not company:
        return None

    gl = frappe.new_doc("GL Entry")
    gl.posting_date = posting_date or nowdate()
    gl.account = account
    gl.debit = flt(debit, 2)
    gl.credit = flt(credit, 2)
    gl.voucher_type = voucher_type
    gl.voucher_no = voucher_no
    gl.remarks = remarks
    gl.company = company
    if party_type:
        gl.party_type = party_type
    if party:
        gl.party = party
    # ERPNext requires cost_center for Profit & Loss accounts.
    # Check if the account is a P&L type (Income/Expense).
    if not cost_center:
        acct_info = frappe.db.get_value("Account", account, ["root_type", "is_group"])
        if acct_info and acct_info[0] in ("Income", "Expense") and not acct_info[1]:
            cost_center = _get_default_cost_center(company)
    if cost_center:
        gl.cost_center = cost_center
    # ignore_links: GL entries may be created before the referenced voucher
    # is fully persisted (e.g. during a multi-step settlement flow).
    gl.insert(ignore_permissions=True, ignore_links=True)
    return gl


def create_gl_entries_batch(entries, voucher_type="Payment Entry", voucher_no="",
                           remarks="", posting_date=None):
    """Create multiple GL Entries in a batch."""
    created = []
    for entry in entries:
        gl = create_gl_entry(
            account=entry["account"],
            debit=entry.get("debit", 0),
            credit=entry.get("credit", 0),
            voucher_type=voucher_type,
            voucher_no=voucher_no,
            remarks=remarks,
            party_type=entry.get("party_type"),
            party=entry.get("party"),
            posting_date=posting_date,
        )
        if gl:
            created.append(gl)
    return created


# ── Sales Invoice GL Entries ─────────────────────────────────────────────────
# When a vendor fulfills an order, they generate a Sales Invoice to the customer.
# The platform coupon and loyalty points are NOT deducted from taxable base
# because SaathiMart will reimburse the vendor for them.

def create_vendor_sales_invoice_gl(vendor_order_id, items, grand_total, tax_amount=0):
    """
    Create GL Entries for vendor's Sales Invoice to customer.
    
    Taxable Product Base = Sum of item amounts (vendor coupon deducted, platform coupon NOT)
    Product VAT = 13% of taxable base
    Total Product Gross Receivable = Taxable + VAT
    
    DR: SaathiMart Clearing Account (amount owed by platform)
    CR: Product Sales Revenue
    CR: Output VAT Liability
    """
    # Avoid double-entry
    if frappe.db.exists("GL Entry", {
        "voucher_no": vendor_order_id,
        "voucher_type": "Sales Invoice",
    }):
        return

    entries = []
    posting_date = nowdate()

    # Revenue credit
    revenue_account = _get_account("revenue")
    if revenue_account:
        entries.append({
            "account": revenue_account,
            "debit": 0,
            "credit": flt(grand_total - tax_amount, 2),
            "remarks": f"Sales from {vendor_order_id}",
        })

    # VAT credit
    vat_account = _get_account("vat_output")
    if vat_account and tax_amount > 0:
        entries.append({
            "account": vat_account,
            "debit": 0,
            "credit": flt(tax_amount, 2),
            "remarks": f"Output VAT for {vendor_order_id}",
        })

    # Clearing Account debit (platform owes this to vendor)
    clearing_account = _get_account("clearing_platform")
    if clearing_account:
        entries.append({
            "account": clearing_account,
            "debit": flt(grand_total, 2),
            "credit": 0,
            "remarks": f"Receivable from SaathiMart for {vendor_order_id}",
        })

    if entries:
        create_gl_entries_batch(
            entries,
            voucher_type="Sales Invoice",
            voucher_no=vendor_order_id,
            remarks=f"Vendor sales invoice for {vendor_order_id}",
            posting_date=posting_date,
        )# ── Settlement Journal Entry ────────────────────────────────────────────────
# When the platform pays (settles) the vendor — this is when cash actually
# moves. The payment.received event does NOT create a Payment Entry because
# the vendor never receives cash from the customer directly.

def create_settlement_journal_entry(vendor_order_id, settlement_amount,
                                     commission_amount=0, reference=""):
    """
    Create a Journal Entry when the platform settles (pays) the vendor.

    Three-party clearing house model:
      DR: Bank/Cash                          (money received from platform)
      DR: Marketplace Commission Expense       (platform's cut)
      CR: SaathiMart Clearing Account          (clears the receivable)

    This is the ONLY time cash hits the vendor's books.
    """
    # Avoid double-entry
    if frappe.db.exists("GL Entry", {
        "voucher_no": vendor_order_id,
        "voucher_type": "Journal Entry",
        "remarks": ["like", "%Settlement%"],
    }):
        return

    entries = []
    posting_date = nowdate()

    bank_account = _get_account("cash_bank")
    clearing_account = _get_account("clearing_platform")
    commission_account = _get_account("commission_expense")

    # Bank/Cash debit — actual money received
    if bank_account:
        entries.append({
            "account": bank_account,
            "debit": flt(settlement_amount, 2),
            "credit": 0,
            "remarks": f"Settlement received from SaathiMart for {vendor_order_id}",
        })

    # Commission expense debit
    if commission_account and flt(commission_amount) > 0:
        entries.append({
            "account": commission_account,
            "debit": flt(commission_amount, 2),
            "credit": 0,
            "remarks": f"Commission deducted for {vendor_order_id}",
        })

    # Clearing Account credit — clears the full receivable
    if clearing_account:
        total_receivable = flt(settlement_amount) + flt(commission_amount)
        entries.append({
            "account": clearing_account,
            "debit": 0,
            "credit": flt(total_receivable, 2),
            "remarks": f"Clearing receivable for {vendor_order_id}",
        })

    if entries:
        create_gl_entries_batch(
            entries,
            voucher_type="Journal Entry",
            voucher_no=vendor_order_id,
            remarks=f"Settlement for {vendor_order_id}" + (f" (ref: {reference})" if reference else ""),
            posting_date=posting_date,
        )


# ── Commission Expense GL Entries ────────────────────────────────────────────

def record_commission_expense(vendor_order_id, commission_amount, commission_pct):
    """
    Record marketplace commission as an expense for the vendor.
    
    DR: Marketplace Commission Expense
    CR: SaathiMart Clearing Account
    """
    if flt(commission_amount) <= 0:
        return

    entries = []
    posting_date = nowdate()

    commission_account = _get_account("commission_expense")
    clearing_account = _get_account("clearing_platform")

    if commission_account and clearing_account:
        entries.append({
            "account": commission_account,
            "debit": flt(commission_amount, 2),
            "credit": 0,
            "remarks": f"Commission ({commission_pct}%) for {vendor_order_id}",
        })
        entries.append({
            "account": clearing_account,
            "debit": 0,
            "credit": flt(commission_amount, 2),
            "remarks": f"Commission payable to SaathiMart for {vendor_order_id}",
        })

        create_gl_entries_batch(
            entries,
            voucher_type="Journal Entry",
            voucher_no=vendor_order_id,
            remarks=f"Commission expense for {vendor_order_id}",
            posting_date=posting_date,
        )


# ── Platform Coupon Reimbursement GL Entries ─────────────────────────────────

def record_platform_coupon_reimbursement(vendor_order_id, coupon_amount):
    """
    When platform absorbs a coupon, vendor gets reimbursed.
    
    DR: SaathiMart Clearing Account (platform pays vendor)
    CR: Platform Coupon Reimbursement (income for vendor)
    """
    if flt(coupon_amount) <= 0:
        return

    entries = []
    posting_date = nowdate()

    clearing_account = _get_account("clearing_platform")
    coupon_income = _get_account("platform_coupon_income")

    if clearing_account and coupon_income:
        entries.append({
            "account": clearing_account,
            "debit": flt(coupon_amount, 2),
            "credit": 0,
            "remarks": f"Coupon reimbursement for {vendor_order_id}",
        })
        entries.append({
            "account": coupon_income,
            "debit": 0,
            "credit": flt(coupon_amount, 2),
            "remarks": f"Platform coupon reimbursement for {vendor_order_id}",
        })

        create_gl_entries_batch(
            entries,
            voucher_type="Journal Entry",
            voucher_no=vendor_order_id,
            remarks=f"Platform coupon reimbursement for {vendor_order_id}",
            posting_date=posting_date,
        )


# ── Loyalty Reimbursement GL Entries ─────────────────────────────────────────

def record_loyalty_reimbursement(vendor_order_id, loyalty_amount):
    """
    When customer redeems loyalty points, platform reimburses vendor.
    
    DR: SaathiMart Clearing Account (platform pays vendor)
    CR: Loyalty Reimbursement (income for vendor)
    """
    if flt(loyalty_amount) <= 0:
        return

    entries = []
    posting_date = nowdate()

    clearing_account = _get_account("clearing_platform")
    loyalty_income = _get_account("loyalty_income")

    if clearing_account and loyalty_income:
        entries.append({
            "account": clearing_account,
            "debit": flt(loyalty_amount, 2),
            "credit": 0,
            "remarks": f"Loyalty reimbursement for {vendor_order_id}",
        })
        entries.append({
            "account": loyalty_income,
            "debit": 0,
            "credit": flt(loyalty_amount, 2),
            "remarks": f"Loyalty points reimbursement for {vendor_order_id}",
        })

        create_gl_entries_batch(
            entries,
            voucher_type="Journal Entry",
            voucher_no=vendor_order_id,
            remarks=f"Loyalty reimbursement for {vendor_order_id}",
            posting_date=posting_date,
        )


# ── Whitelisted API Endpoints ────────────────────────────────────────────────

@frappe.whitelist()
def get_vendor_gl_entries(vendor_order_id=None, from_date=None, to_date=None):
    """Get GL entries for this vendor, optionally filtered by order and date range."""
    filters = {}
    if vendor_order_id:
        filters["voucher_no"] = vendor_order_id
    if from_date and to_date:
        filters["posting_date"] = ["between", [from_date, to_date]]

    return frappe.get_all(
        "GL Entry",
        filters=filters,
        fields=["name", "posting_date", "account", "debit", "credit",
                "voucher_type", "voucher_no", "remarks"],
        order_by="posting_date asc, creation asc",
    )


@frappe.whitelist()
def get_vendor_clearing_balance():
    """Get the current clearing account balance (what platform owes vendor)."""
    clearing_account = _get_account("clearing_platform")
    if not clearing_account:
        return {"balance": 0, "error": "Clearing account not found"}

    balance = frappe.db.sql("""
        SELECT SUM(debit) - SUM(credit) as balance
        FROM `tabGL Entry`
        WHERE account = %s
    """, (clearing_account,), as_dict=True)

    return {
        "balance": round(flt(balance[0].balance) if balance else 0, 2),
        "account": clearing_account,
    }

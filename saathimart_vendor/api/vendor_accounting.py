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
from frappe.utils import flt, nowdate, rounded


# ── Vendor Chart of Accounts ─────────────────────────────────────────────────
# These accounts MUST exist in the vendor's ERPNext Chart of Accounts.

VENDOR_ACCOUNTS = {
    "cash_bank":              "Cash/Bank",
    "revenue":                "Sales",
    "vat_output":             "Output VAT",
    "vat_input":              "Input VAT",
    "clearing_platform":      "SaathiMart Clearing",
    "tds_payable":            "TDS Payable",
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
    "tds_payable":            ["TDS Payable", "TDS", "Duties and Taxes"],
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

    Multi-company safety: a vendor site can hold several Companies (test +
    load + production). The resolved account MUST belong to the company
    _get_company() returns, otherwise ERPNext rejects the GL Entry with
    "Account X does not belong to Company Y". Every lookup is therefore
    company-scoped, and the cache key includes the company.
    """
    company = _get_company()
    if not company:
        return None

    cache_key = f"{company}:{account_key}"
    cached = _account_cache.get(cache_key)
    if cached and frappe.db.exists("Account", cached):
        return cached

    # Search by the mapped name (handles company suffix)
    search_name = _ACCOUNT_NAME_MAP.get(account_key, VENDOR_ACCOUNTS.get(account_key, ""))
    if not search_name:
        frappe.log_error(f"Vendor account {account_key} not found", "Vendor Accounting")
        return None

    # Prefer leaf accounts in THIS company
    found = frappe.db.get_value(
        "Account",
        {"account_name": ["like", f"{search_name}%"], "is_group": 0, "company": company},
        "name",
    )
    if found:
        _account_cache[cache_key] = found
        return found

    # Marketplace-specific accounts: provision on demand BEFORE the fuzzy
    # fallback — fuzzy matching for e.g. clearing_platform would otherwise
    # degrade to Debtors (a Receivable, wrong type: ERPNext then demands a
    # Customer party and settlement clearing breaks).
    provisioned = _provision_account(account_key, company)
    if provisioned:
        _account_cache[cache_key] = provisioned
        return provisioned

    # Fuzzy fallback — prefer non-group, still company-scoped
    for keyword in _FUZZY_FALLBACKS.get(account_key, []):
        found = frappe.db.get_value(
            "Account",
            {"account_name": ["like", f"%{keyword}%"], "is_group": 0, "company": company},
            "name",
        )
        if found:
            _account_cache[cache_key] = found
            return found

    frappe.log_error(f"Vendor account {account_key} does not exist for company {company}", "Vendor Accounting")
    return None


# Per-company account resolution cache (see _get_account)
_account_cache: dict = {}


# ── Marketplace chart provisioning ───────────────────────────────────────────
# Standard ERPNext charts have no marketplace accounts (SaathiMart Clearing,
# TDS Payable on commission, marketplace commission expense...). Without them
# _get_account used to silently degrade: clearing fell back to Debtors (a
# Receivable — ERPNext then demands a Customer party on every GL row) and
# TDS Payable resolved to None, dropping the withholding booking entirely.
# These keys are provisioned on demand under the correct parents instead.
#
# SaathiMart Clearing is deliberately a plain Current Asset, NOT a
# Receivable — the platform is not a ERPNext "Customer"; it is a clearing
# counterparty whose balance nets against settlement payouts.
_PROVISIONABLE = {
    "clearing_platform": {
        "account_name": "SaathiMart Clearing",
        "parents": ["Current Assets", "Current Asset"],
        "root_type": "Asset",
        "account_type": None,
    },
    "tds_payable": {
        "account_name": "TDS Payable",
        "parents": ["Duties and Taxes", "Current Liabilities"],
        "root_type": "Liability",
        "account_type": "Tax",
    },
    "vat_output": {
        "account_name": "Output VAT",
        "parents": ["Duties and Taxes", "Current Liabilities"],
        "root_type": "Liability",
        "account_type": "Tax",
    },
    "commission_expense": {
        "account_name": "Marketplace Commission",
        "parents": ["Indirect Expenses", "Direct Expenses", "Expenses"],
        "root_type": "Expense",
        "account_type": "Expense Account",
    },
    "platform_coupon_income": {
        "account_name": "Platform Coupon Reimbursement",
        "parents": ["Indirect Income", "Direct Income", "Income"],
        "root_type": "Income",
        "account_type": "Income Account",
    },
    "loyalty_income": {
        "account_name": "Loyalty Reimbursement",
        "parents": ["Indirect Income", "Direct Income", "Income"],
        "root_type": "Income",
        "account_type": "Income Account",
    },
}


# ── Platform (Entity A) account resolution ───────────────────────────────
# The hub pushes its own ledger postings (platform.ledger_entry events) to
# whichever vendor site is designated SaathiMart Settings > Platform Ledger
# Vendor. Those accounts describe the PLATFORM's books, not this vendor's,
# and standard charts don't carry them — resolved/provisioned under a
# dedicated "SaathiMart Platform" name prefix so they can never collide
# with this site's own vendor-book accounts (which share generic names
# like "TDS Payable").

PLATFORM_ACCOUNT_FALLBACKS = {
    "cash_bank":               ["Cash/Bank"],
    "revenue":                 ["Product Revenue"],
    "commission_income":       ["Marketplace Commission"],
    "platform_coupon_exp":     ["Platform Coupon Expense"],
    "loyalty_expense":         ["Loyalty Points Expense"],
    "delivery_income":         ["Delivery Service Income"],
    "clearing_vendor":         ["Clearing Account - Vendor"],
    "clearing_logistics":      ["Clearing Account - Logistics"],
    "tds_receivable":          ["TDS Receivable"],
    "tds_payable":             ["TDS Payable"],
    "platform_coupon_payable": ["Platform Coupon Payable"],
    "loyalty_payable":         ["Loyalty Payable"],
    "vat_output":              ["Output VAT"],
    "vat_input":               ["Input VAT"],
    "accounts_receivable":     ["Accounts Receivable"],
    "accounts_payable":        ["Accounts Payable"],
}

_PLATFORM_PROVISIONABLE = {
    "cash_bank":        {"parents": ["Current Assets", "Current Asset"], "root_type": "Asset", "account_type": "Bank"},
    "clearing_vendor":  {"parents": ["Current Assets", "Current Asset"], "root_type": "Asset", "account_type": None},
    "clearing_logistics": {"parents": ["Current Assets", "Current Asset"], "root_type": "Asset", "account_type": None},
    "platform_coupon_payable": {"parents": ["Current Liabilities"], "root_type": "Liability", "account_type": None},
    "loyalty_payable":  {"parents": ["Current Liabilities"], "root_type": "Liability", "account_type": None},
    "tds_receivable":   {"parents": ["Current Assets", "Current Asset"], "root_type": "Asset", "account_type": "Tax"},
    "tds_payable":      {"parents": ["Duties and Taxes", "Current Liabilities"], "root_type": "Liability", "account_type": "Tax"},
    "commission_income": {"parents": ["Indirect Income", "Direct Income", "Income"], "root_type": "Income", "account_type": "Income Account"},
    "platform_coupon_exp": {"parents": ["Indirect Expenses", "Direct Expenses", "Expenses"], "root_type": "Expense", "account_type": "Expense Account"},
    "loyalty_expense":  {"parents": ["Indirect Expenses", "Direct Expenses", "Expenses"], "root_type": "Expense", "account_type": "Expense Account"},
    "delivery_income":  {"parents": ["Direct Income", "Income"], "root_type": "Income", "account_type": "Income Account"},
    "revenue":          {"parents": ["Direct Income", "Income"], "root_type": "Income", "account_type": "Income Account"},
    "vat_output":       {"parents": ["Duties and Taxes", "Current Liabilities"], "root_type": "Liability", "account_type": "Tax"},
    "vat_input":        {"parents": ["Duties and Taxes", "Current Assets"], "root_type": "Asset", "account_type": "Tax"},
    "accounts_receivable": {"parents": ["Current Assets", "Current Asset"], "root_type": "Asset", "account_type": "Receivable"},
    "accounts_payable": {"parents": ["Current Liabilities"], "root_type": "Liability", "account_type": "Payable"},
}


def get_platform_account(account_key):
    """Resolve (or lazily provision) the PLATFORM's account for account_key.

    Mirrors _get_account but against the "SaathiMart Platform"-prefixed
    chart. Returns None (caller fails closed) if the key is unknown or
    provisioning is impossible on this chart.
    """
    company = _get_company()
    if not company:
        return None
    key = f"platform:{company}:{account_key}"
    cached = _account_cache.get(key)
    if cached and frappe.db.exists("Account", cached):
        return cached

    names = PLATFORM_ACCOUNT_FALLBACKS.get(account_key) or []
    prefix = "SaathiMart Platform"
    for name in names:
        found = frappe.db.get_value(
            "Account",
            {"account_name": ["like", f"{prefix} {name}%"], "is_group": 0, "company": company},
            "name",
        )
        if found:
            _account_cache[key] = found
            return found

    spec = _PLATFORM_PROVISIONABLE.get(account_key)
    if not spec or not names:
        return None
    parent = None
    for parent_name in spec["parents"]:
        parent = frappe.db.get_value(
            "Account",
            {"account_name": ["like", f"{parent_name}%"], "is_group": 1, "company": company},
            "name",
        )
        if parent:
            break
    if not parent:
        return None
    try:
        acc = frappe.new_doc("Account")
        acc.account_name = f"{prefix} {names[0]}"
        acc.company = company
        acc.parent_account = parent
        acc.is_group = 0
        acc.root_type = spec["root_type"]
        acc.report_type = (
            "Profit and Loss" if spec["root_type"] in ("Income", "Expense")
            else "Balance Sheet"
        )
        if spec["account_type"]:
            acc.account_type = spec["account_type"]
        acc.insert(ignore_permissions=True)
        _account_cache[key] = acc.name
        return acc.name
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"platform account provisioning failed: {account_key} for {company}",
        )
        return None


def _provision_account(account_key: str, company: str) -> str | None:
    """Create a missing marketplace account under the right parent group."""
    spec = _PROVISIONABLE.get(account_key)
    if not spec:
        return None

    parent = None
    for parent_name in spec["parents"]:
        parent = frappe.db.get_value(
            "Account",
            {"account_name": ["like", f"{parent_name}%"], "is_group": 1, "company": company},
            "name",
        )
        if parent:
            break
    if not parent:
        return None

    try:
        acc = frappe.new_doc("Account")
        acc.account_name = spec["account_name"]
        acc.company = company
        acc.parent_account = parent
        acc.is_group = 0
        acc.root_type = spec["root_type"]
        acc.report_type = (
            "Profit and Loss" if spec["root_type"] in ("Income", "Expense")
            else "Balance Sheet"
        )
        if spec["account_type"]:
            acc.account_type = spec["account_type"]
        acc.insert(ignore_permissions=True)
        return acc.name
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"provision_account failed: {spec['account_name']} for {company}",
        )
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
    # Idempotency guard: identical (account, voucher, amount, remarks) rows
    # must never double-book. Both event transports (webhook + Redis
    # Streams) are at-least-once and can race the same order; without this
    # a replayed delivery duplicates the voucher. Re-marking prevents the
    # pair from being written twice within one uncommitted transaction too.
    if frappe.db.exists("GL Entry", {
        "voucher_no": voucher_no or "",
        "account": account,
        "debit": flt(debit, 2),
        "credit": flt(credit, 2),
        "remarks": remarks or "",
    }):
        return None
    frappe.flags.setdefault("sm_gl_seen", set())
    seen_key = (voucher_no or "", account, flt(debit, 2), flt(credit, 2), remarks or "")
    if seen_key in frappe.flags.sm_gl_seen:
        return None
    frappe.flags.sm_gl_seen.add(seen_key)

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

def create_vendor_sales_invoice_gl(vendor_order_id, grand_total, tax_amount=0):
    """
    Create GL Entries for vendor's Sales Invoice to customer.

    Taxable Product Base = Sum of item amounts (vendor coupon deducted, platform coupon NOT)
    Product VAT = 13% of taxable base
    Total Product Gross Receivable = Taxable + VAT

    DR: SaathiMart Clearing Account (amount owed by platform)
    CR: Product Sales Revenue
    CR: Output VAT Liability

    Fail-closed: all legs are resolved BEFORE anything is written.
    create_gl_entry posts raw GL documents with no debit=credit
    enforcement, and _get_account reports unresolvable accounts via
    log_error instead of raising — a leg-by-leg guard could therefore
    post DR clearing with no matching credits (books silently broken).
    If any required leg cannot be resolved, NOTHING posts.
    """
    # Avoid double-entry
    if frappe.db.exists("GL Entry", {
        "voucher_no": vendor_order_id,
        "voucher_type": "Sales Invoice",
    }):
        return

    # ── Fail-closed pre-check: resolve every required leg first ──────────
    clearing_account = _get_account("clearing_platform")
    revenue_account = _get_account("revenue")
    if not (clearing_account and revenue_account):
        frappe.log_error(
            f"Sales invoice GL for {vendor_order_id} aborted: "
            f"clearing={clearing_account}, revenue={revenue_account} "
            f"(company {_get_company()}) — nothing posted (fail-closed)",
            "Vendor Accounting",
        )
        return

    vat_account = None
    if flt(tax_amount, 2) > 0:
        vat_account = _get_account("vat_output")
        if not vat_account:
            frappe.log_error(
                f"Sales invoice GL for {vendor_order_id} aborted: no Output VAT "
                f"account (company {_get_company()}) — nothing posted (fail-closed)",
                "Vendor Accounting",
            )
            return

    # ── Build all legs (amounts derived from the SAME grand_total) ───────
    taxable_value = rounded(flt(grand_total, 2) - flt(tax_amount, 2), 2)
    if taxable_value < 0:
        frappe.log_error(
            f"Sales invoice GL for {vendor_order_id} aborted: VAT {tax_amount} "
            f"exceeds grand_total {grand_total} — nothing posted (fail-closed)",
            "Vendor Accounting",
        )
        return

    entries = [
        {
            "account": clearing_account,
            "debit": flt(grand_total, 2),
            "credit": 0,
            "remarks": f"Receivable from SaathiMart for {vendor_order_id}",
        },
        {
            "account": revenue_account,
            "debit": 0,
            "credit": taxable_value,
            "remarks": f"Sales from {vendor_order_id}",
        },
    ]
    if flt(tax_amount, 2) > 0:
        entries.append({
            "account": vat_account,
            "debit": 0,
            "credit": flt(tax_amount, 2),
            "remarks": f"Output VAT for {vendor_order_id}",
        })

    # ── Hard balance assertion: never post one-sided GL rows ─────────────
    total_dr = sum(flt(e["debit"]) for e in entries)
    total_cr = sum(flt(e["credit"]) for e in entries)
    if abs(total_dr - total_cr) > 0.05:
        frappe.log_error(
            f"Sales invoice GL for {vendor_order_id} aborted: unbalanced "
            f"Dr {total_dr} vs Cr {total_cr} — nothing posted (fail-closed)",
            "Vendor Accounting",
        )
        return

    create_gl_entries_batch(
        entries,
        voucher_type="Sales Invoice",
        voucher_no=vendor_order_id,
        remarks=f"Vendor sales invoice for {vendor_order_id}",
        posting_date=nowdate(),
    )


# ── Settlement Journal Entry ────────────────────────────────────────────────
# When the platform pays (settles) the vendor — this is when cash actually
# moves. The payment.received event does NOT create a Payment Entry because
# the vendor never receives cash from the customer directly.

def create_settlement_journal_entry(vendor_order_id, settlement_amount,
                                     commission_amount=0, reference="",
                                     tds_amount=0):
    """
    Create a Journal Entry when the platform settles (pays) the vendor.

    Commission expense and TDS were already recognised at order time (see
    record_commission_expense / record_tds_withheld) — this entry only
    moves money:

      DR: Bank/Cash                    (cash actually received)
      CR: SaathiMart Clearing Account  (clears the receivable)

    `settlement_amount` from the hub is the GROSS payout (net + the s88
    TDS the vendor withheld from the platform's commission bill — the
    platform pays the withheld sum out too and keeps the certificate as
    ITS TDS Receivable). The vendor's clearing receivable nets to exactly
    this gross figure (sale at full base − commission bill incl. its VAT
    + withheld TDS), so this entry zeroes it to the paisa.
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

    # Bank/Cash debit — actual money received
    if bank_account:
        entries.append({
            "account": bank_account,
            "debit": flt(settlement_amount, 2),
            "credit": 0,
            "remarks": f"Settlement received from SaathiMart for {vendor_order_id}",
        })

    # Clearing Account credit — clears the receivable
    if clearing_account:
        entries.append({
            "account": clearing_account,
            "debit": 0,
            "credit": flt(settlement_amount, 2),
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


# ── TDS Withheld on Commission (Income Tax Act 2058, s88) ───────────────────
# The commission the vendor pays SaathiMart is a service charge, so the
# vendor must withhold 15% of it and deposit it with IRD. Recorded at the
# same moment the commission expense is booked, so the liability never
# exists without its withholding.

def record_tds_withheld(vendor_order_id, commission_amount, tds_rate=15.0):
    """
    DR: SaathiMart Clearing   (platform's commission receivable shrinks — the
                              15% the vendor withholds never leaves for the
                              platform; it goes to IRD on the vendor's behalf)
    CR: TDS Payable           (owed to IRD until the certificate settles it)

    Net effect: commission expense still shows gross, TDS Payable shows what
    the vendor must deposit with IRD, and the clearing balance drops by the
    withheld amount — so settlement pays out cash minus TDS, exactly like
    the hub's settlement JE expects. The previous version credited the
    commission expense account a second time, which double-counted income
    and left every TDS voucher unbalanced by exactly the TDS amount.
    """
    tds = rounded(flt(commission_amount) * flt(tds_rate) / 100.0, 2)
    if tds <= 0:
        return

    if frappe.db.exists("GL Entry", {
        "voucher_no": vendor_order_id,
        "voucher_type": "Journal Entry",
        "remarks": ["like", "%TDS withheld%"],
    }):
        return

    tds_account = _get_account("tds_payable")
    clearing_account = _get_account("clearing_platform")
    if not (tds_account and clearing_account):
        return

    create_gl_entries_batch([
        {
            "account": tds_account,
            "debit": 0,
            "credit": tds,
            "remarks": f"TDS withheld on SaathiMart commission for {vendor_order_id}",
        },
        {
            "account": clearing_account,
            "debit": tds,
            "credit": 0,
            "remarks": f"TDS withheld on commission (s88) for {vendor_order_id}",
        },
    ], voucher_type="Journal Entry",
       voucher_no=vendor_order_id,
       remarks=f"TDS withheld on commission for {vendor_order_id}")


# ── Commission Expense GL Entries ────────────────────────────────────────────

def record_commission_expense(vendor_order_id, commission_amount, commission_pct):
    """
    Record marketplace commission as an expense for the vendor, with the
    13% input VAT on the platform's commission bill (the platform invoices
    commission + service VAT; the vendor claims the VAT as input credit): 

    DR: Marketplace Commission Expense   (net commission)
    DR: Input VAT                        (13% of commission)
    CR: SaathiMart Clearing Account      (gross payable to platform)
    """
    if flt(commission_amount) <= 0:
        return

    entries = []
    posting_date = nowdate()

    commission_account = _get_account("commission_expense")
    clearing_account = _get_account("clearing_platform")
    input_vat_account = _get_account("vat_input")

    if commission_account and clearing_account:
        commission_amount = flt(commission_amount, 2)
        input_vat = rounded(commission_amount * 13.0 / 100.0, 2)
        entries.append({
            "account": commission_account,
            "debit": commission_amount,
            "credit": 0,
            "remarks": f"Commission ({commission_pct}%) for {vendor_order_id}",
        })
        if input_vat > 0 and input_vat_account:
            entries.append({
                "account": input_vat_account,
                "debit": input_vat,
                "credit": 0,
                "remarks": f"Input VAT on commission bill for {vendor_order_id}",
            })
        entries.append({
            "account": clearing_account,
            "debit": 0,
            "credit": commission_amount + input_vat,
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
def ensure_platform_party(party_type, party):
	"""Ensure a GL-referenced party exists on this site; upsert if missing.

	Platform ledger events carry Customer parties that exist only on the
	hub — ERPNext's GL Entry.validate_party fetches the party doc and
	crashes on None if it doesn't exist here. Returns the party name on
	success, or None (caller drops the party refs rather than aborting
	the whole batch — the party link is informational for cash rows).
	"""
	if not party_type or not party:
		return None
	if frappe.db.exists(party_type, party):
		return party
	try:
		if party_type == "Customer":
			c = frappe.new_doc("Customer")
			c.customer_name = party
			c.customer_type = "Individual"
			c.customer_group = (
				frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
				or "All Customer Groups"
			)
			c.territory = (
				frappe.db.get_value("Territory", {"is_group": 0}, "name")
				or "All Territories"
			)
			c.insert(ignore_permissions=True)
			return c.name
		return party
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"platform party upsert failed: {party_type} {party}",
		)
		return None


def create_platform_gl_entries(payload):
    """Receiver for the hub's platform.ledger_entry events.

    The hub (plain Frappe, no ERPNext) computes Entity A's postings —
    commission income, platform coupon/loyalty expense, TDS receivable,
    settlement money movements — and pushes the finished batch here. This
    site keeps those books in ERPNext. Each entry carries a symbolic
    account_key (PLATFORM_ACCOUNTS on the hub side); the real account is
    resolved (or lazily provisioned) under the "SaathiMart Platform"
    name prefix so they can never collide with this site's own vendor-book
    accounts.

    Idempotent: create_gl_entry dedupes on (account, voucher, amounts,
    remarks); replayed deliveries are no-ops. Fails closed per batch:
    any account that cannot be resolved aborts the whole voucher with
    nothing posted (a half-posted platform ledger would be worse than a
    delayed one — the hub's drain retries).
    """
    voucher_type = payload.get("voucher_type") or "Journal Entry"
    voucher_no = payload.get("voucher_no") or ""
    remarks = payload.get("remarks") or ""
    entries = payload.get("entries") or []
    if not voucher_no or not entries:
        frappe.log_error(
            f"platform.ledger_entry missing voucher_no or entries: {payload}",
            "Platform Ledger Receiver",
        )
        return {"ok": False, "reason": "malformed"}

    # Resolve all accounts FIRST — fail closed before writing anything.
    resolved = []
    for e in entries:
        account = get_platform_account(e.get("account_key") or "")
        if not account:
            frappe.log_error(
                f"platform.ledger_entry {voucher_no}: account_key "
                f"{e.get('account_key')} unresolved — batch aborted, nothing posted",
                "Platform Ledger Receiver",
            )
            return {"ok": False, "reason": f"unresolved:{e.get('account_key')}"}
        resolved.append({**e, "account": account})

    # Parties: upsert missing Customers (hub parties don't exist here).
    # A party that can't be ensured loses its link but keeps its row —
    # informational for cash entries, not worth aborting the batch.
    for e in resolved:
        if e.get("party"):
            ensured = ensure_platform_party(e.get("party_type"), e.get("party"))
            if not ensured:
                e.pop("party", None)
                e.pop("party_type", None)
            else:
                e["party"] = ensured

    total_dr = rounded(sum(flt(e.get("debit")) for e in resolved), 2)
    total_cr = rounded(sum(flt(e.get("credit")) for e in resolved), 2)
    if abs(total_dr - total_cr) > 0.05:
        frappe.log_error(
            f"platform.ledger_entry {voucher_no} UNBALANCED: Dr {total_dr} vs Cr {total_cr} "
            f"— batch aborted, nothing posted",
            "Platform Ledger Receiver",
        )
        return {"ok": False, "reason": "unbalanced"}

    for e in resolved:
        create_gl_entry(
            account=e["account"],
            debit=e.get("debit", 0),
            credit=e.get("credit", 0),
            voucher_type=voucher_type,
            voucher_no=voucher_no,
            remarks=e.get("remarks") or remarks,
            party_type=e.get("party_type"),
            party=e.get("party"),
        )
    return {"ok": True, "entries": len(resolved)}


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

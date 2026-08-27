import json
import frappe
from frappe import _
from frappe.model.document import Document
from saathimart_vendor.utils import enqueue_outbox, get_config, generate_event_id, next_event_seq, VALID_TRANSITIONS, create_payment_entry_for_order


class VendorOrder(Document):

    def validate(self):
        if self.status and self.is_new():
            return
        if self.status and self.get_db_value("status") and self.status != self.get_db_value("status"):
            old = self.get_db_value("status")
            allowed = VALID_TRANSITIONS.get(old, [])
            if self.status not in allowed:
                frappe.throw(_(
                    "Invalid status transition: {0} → {1}. Allowed: {2}"
                ).format(old, self.status, ", ".join(allowed) if allowed else "none (terminal)"))

    @frappe.whitelist()
    def accept_order(self):
        if self.status != "Received":
            frappe.throw(_(f"Cannot accept order in status: {self.status}"))
        if self.payment_method == "eSewa" and self.payment_status != "Paid":
            frappe.throw(_("Cannot accept eSewa order until payment is confirmed."))
        config = get_config()
        if not config:
            frappe.throw(_("Vendor Config not configured"))
        if not self.items:
            frappe.throw(_("Order has no items"))

        # Resolve the fulfillment warehouse: use the hub-assigned warehouse
        # if present, otherwise fall back to the vendor's default.
        fulfillment_warehouse = self.warehouse or config.default_warehouse
        if self.warehouse and not self.erpnext_warehouse:
            # Look up the ERPNext warehouse name from our warehouse table
            wh_row = None
            for wh in (config.warehouses or []):
                if wh.warehouse_name == self.warehouse and wh.erpnext_warehouse:
                    wh_row = wh
                    break
            if wh_row:
                self.erpnext_warehouse = wh_row.erpnext_warehouse
                fulfillment_warehouse = wh_row.erpnext_warehouse

        so = frappe.new_doc("Sales Order")
        so.customer = _get_or_create_customer(self.customer_name, self.customer_phone)
        so.delivery_date = frappe.utils.add_days(frappe.utils.today(), 1)
        for item in self.items:
            mapping = frappe.db.get_value(
                "Product Mapping",
                {"hub_product_id": item.product},
                ["item_code"], as_dict=True,
            )
            if not mapping:
                frappe.throw(_(
                    f"No mapping for hub product {item.product}. "
                    "Create a Product Mapping first."
                ))
            so.append("items", {
                "item_code": mapping.item_code,
                "qty": item.qty,
                "rate": item.rate,
                "warehouse": fulfillment_warehouse,
            })
        # Price List / currency / exchange rate aren't set anywhere above —
        # normally the desk UI's client script fills these in as items are
        # added; built server-side like this, they're left blank and
        # insert() fails on mandatory fields without this.
        so.set_missing_values()
        so.insert(ignore_permissions=True)
        # Suppress event_handlers.orders.on_sales_order_submit for this
        # submit — this method enqueues its own order.confirmed below, and
        # without the flag both would fire in flows where self.sales_order
        # is already persisted (e.g. retrying accept after a prior failed
        # save left the link in place).
        frappe.flags.in_vendor_order_accept = True
        try:
            so.submit()
        finally:
            frappe.flags.in_vendor_order_accept = False

        self.sales_order = so.name
        self.status = "Accepted"
        self.accepted_at = frappe.utils.now_datetime()
        self.save(ignore_permissions=True)

        # Prepaid order (eSewa) accepted after the hub's payment.received
        # event flipped payment_status — the money is real, so record it as
        # a Payment Entry against this fresh Sales Order right now instead
        # of leaving the receivable open on the books.
        if self.payment_status == "Paid" and not self.payment_entry:
            create_payment_entry_for_order(self, reference_no=self.payment_reference or "")

        enqueue_outbox(
            event_type="order.confirmed",
            payload={
                "order_id": self.hub_order_id,
                "vendor_id": config.vendor_id,
                "sales_order": so.name,
                "event_id": generate_event_id(),
                "event_seq": next_event_seq(),
            },
            voucher_type="Sales Order", voucher_no=so.name,
        )
        return {"sales_order": so.name}

    @frappe.whitelist()
    def mark_preparing(self):
        """
        Optional intermediate step between Accepted and Dispatched — a
        vendor packing a larger order can flag it as being worked on rather
        than jumping straight from Accepted to Dispatched. Previously this
        was a legal transition in VALID_TRANSITIONS with no way to actually
        reach it — dead code hiding behind a state machine that claimed to
        support it.
        """
        if self.status != "Accepted":
            frappe.throw(_(f"Cannot mark preparing in status: {self.status}"))
        config = get_config()
        self.status = "Preparing"
        self.preparing_at = frappe.utils.now_datetime()
        self.save(ignore_permissions=True)
        enqueue_outbox(
            event_type="order.preparing",
            payload={
                "order_id": self.hub_order_id,
                "vendor_id": config.vendor_id if config else "",
                "event_id": generate_event_id(),
                "event_seq": next_event_seq(),
            },
            voucher_type="Vendor Order", voucher_no=self.name,
        )

    @frappe.whitelist()
    def mark_dispatched(self):
        """
        Creates and submits a real ERPNext Delivery Note against the linked
        Sales Order, instead of just flipping status. Previously this was a
        pure status flip with no stock document behind it at all — a
        vendor could mark an order Dispatched (and later Delivered,
        deducting the hub's own Vendor Stock ledger) with zero
        corresponding entry in ERPNext's own Stock Ledger, relying entirely
        on the hourly reconciliation job to eventually notice the drift.
        Submitting a real Delivery Note here means the stock movement is
        accounted for immediately, the same way it would be if a warehouse
        worker created the Delivery Note by hand.
        """
        if self.status not in ("Accepted", "Preparing"):
            frappe.throw(_(f"Cannot dispatch in status: {self.status}"))
        if not self.sales_order:
            frappe.throw(_("No linked Sales Order — cannot create a Delivery Note"))
        config = get_config()

        from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

        dn = make_delivery_note(self.sales_order)
        for item in dn.items:
            # This Delivery Note exists to get the stock *quantity*
            # movement recorded (see docstring above) — it isn't standing
            # in for proper accounting. A vendor whose item has never gone
            # through a Purchase Receipt/Stock Entry with a rate has a
            # valuation_rate of 0, and ERPNext refuses to submit a
            # Delivery Note against a zero-valuation item for accounting
            # reasons (COGS needs a rate) unless told it's fine. Forcing
            # that open here means a vendor without fully-configured
            # purchase costing can still dispatch orders — the alternative
            # is every dispatch failing until they go set up valuation.
            item.allow_zero_valuation_rate = 1
        dn.insert(ignore_permissions=True)
        # Suppress event_handlers.orders.on_delivery_note_submit for this
        # submit — this method sets status + enqueues order.dispatched
        # itself below, same reasoning as accept_order()/cancel_order()
        # suppressing their own Sales Order doc_events.
        frappe.flags.in_vendor_order_dispatch = True
        try:
            dn.submit()
        finally:
            frappe.flags.in_vendor_order_dispatch = False

        self.status = "Dispatched"
        self.dispatched_at = frappe.utils.now_datetime()
        self.save(ignore_permissions=True)
        enqueue_outbox(
            event_type="order.dispatched",
            payload={
                "order_id": self.hub_order_id,
                "vendor_id": config.vendor_id if config else "",
                "delivery_note": dn.name,
                "event_id": generate_event_id(),
                "event_seq": next_event_seq(),
            },
            voucher_type="Delivery Note", voucher_no=dn.name,
        )
        return {"delivery_note": dn.name}

    @frappe.whitelist()
    def mark_delivered(self):
        if self.status != "Dispatched":
            frappe.throw(_(f"Cannot mark delivered in status: {self.status}"))
        config = get_config()
        self.status = "Delivered"
        self.delivered_at = frappe.utils.now_datetime()
        self.save(ignore_permissions=True)
        enqueue_outbox(
            event_type="order.delivered",
            payload={
                "order_id": self.hub_order_id,
                "vendor_id": config.vendor_id if config else "",
                "event_id": generate_event_id(),
                "event_seq": next_event_seq(),
            },
            voucher_type="Vendor Order", voucher_no=self.name,
        )

    @frappe.whitelist()
    def cancel_order(self, reason=""):
        if self.status in ("Delivered", "Cancelled"):
            frappe.throw(_(f"Cannot cancel in status: {self.status}"))
        config = get_config()
        if self.sales_order:
            so = frappe.get_doc("Sales Order", self.sales_order)
            if so.docstatus == 1:
                # so.cancel() fires event_handlers.orders.on_sales_order_cancel,
                # which (for Sales Orders cancelled outside this method, e.g.
                # directly via Desk) sets this Vendor Order's status and
                # enqueues order.cancel itself. This method already does both
                # below — with the vendor-supplied `reason` the hook doesn't
                # have — so the flag makes the hook skip entirely and avoid a
                # duplicate event / redundant write.
                frappe.flags.in_vendor_order_cancel = True
                try:
                    so.cancel()
                finally:
                    frappe.flags.in_vendor_order_cancel = False
                # Reload in case anything else touched this doc while
                # so.cancel()'s own hooks/notifications ran.
                self.reload()
        self.status = "Cancelled"
        self.notes = f"Cancelled by vendor: {reason}" if reason else "Cancelled by vendor"
        self.save(ignore_permissions=True)
        enqueue_outbox(
            event_type="order.cancel",
            payload={
                "order_id": self.hub_order_id,
                "vendor_id": config.vendor_id if config else "",
                "reason": reason or "Vendor cancelled",
                "event_id": generate_event_id(),
                "event_seq": next_event_seq(),
            },
            voucher_type="Vendor Order", voucher_no=self.name,
        )


def _first_leaf(doctype):
    """First non-group record of doctype, or None if only the root exists."""
    return frappe.db.get_value(doctype, {"is_group": 0}, "name")


# Customer Group / Territory are both ERPNext NestedSet trees with
# differently-named fields for the same concepts (name field, parent
# field), hence the lookup table rather than hardcoding one or the other.
_TREE_DOCTYPE_CONFIG = {
    "Customer Group": {"name_field": "customer_group_name", "parent_field": "parent_customer_group",
                        "root_name": "All Customer Groups"},
    "Territory": {"name_field": "territory_name", "parent_field": "parent_territory",
                  "root_name": "All Territories"},
}


def _ensure_leaf(doctype, leaf_name):
    """
    Return an existing leaf record of doctype, creating the root node and a
    leaf under it from scratch if the tree is completely empty.

    A fresh vendor site that never went through ERPNext's Setup Wizard
    (this app's own test suite documents that installer as currently
    broken on this ERPNext version, and docker/init.sh doesn't run it
    either) has *zero* Customer Group / Territory records — not even the
    root "All ..." node. The previous fallback assumed "Individual"/"Nepal"
    already existed somewhere; on a genuinely fresh site neither does,
    so every single accept_order() call failed with "Could not find
    Customer Group: Individual, Territory: Nepal" — not an edge case, the
    very first order any vendor ever tried to accept.
    """
    existing = _first_leaf(doctype)
    if existing:
        return existing

    cfg = _TREE_DOCTYPE_CONFIG[doctype]
    root_name = cfg["root_name"]
    if not frappe.db.exists(doctype, root_name):
        root = frappe.new_doc(doctype)
        root.update({cfg["name_field"]: root_name, "is_group": 1})
        root.insert(ignore_permissions=True, ignore_mandatory=True)

    if frappe.db.exists(doctype, leaf_name):
        return leaf_name

    leaf = frappe.new_doc(doctype)
    leaf.update({cfg["name_field"]: leaf_name, "is_group": 0, cfg["parent_field"]: root_name})
    leaf.insert(ignore_permissions=True)
    return leaf.name


def _get_or_create_customer(name, phone):
    existing = frappe.db.get_value("Customer", {"customer_name": name}, "name")
    if existing:
        return existing
    cust = frappe.new_doc("Customer")
    cust.customer_name = name
    cust.customer_type = "Individual"
    # Selling Settings' customer_group/territory are blank on a site where
    # nobody has been through Selling Settings manually (any fresh vendor
    # site) — "All Customer Groups"/"All Territories" are group/root nodes,
    # and ERPNext rejects a non-group Customer being filed under one, so
    # falling back to those would fail every single time. Fall back to a
    # real leaf node instead, creating one if the tree is entirely empty.
    cust.customer_group = (frappe.db.get_single_value("Selling Settings", "customer_group")
        or _ensure_leaf("Customer Group", "Individual"))
    cust.territory = (frappe.db.get_single_value("Selling Settings", "territory")
        or _ensure_leaf("Territory", "Nepal"))
    cust.insert(ignore_permissions=True)
    return cust.name

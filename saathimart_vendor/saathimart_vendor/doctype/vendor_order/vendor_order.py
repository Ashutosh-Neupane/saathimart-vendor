import json
import frappe
from frappe import _
from frappe.model.document import Document
from saathimart_vendor.utils import enqueue_outbox, get_config, generate_event_id, next_event_seq, VALID_TRANSITIONS


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
                "warehouse": config.default_warehouse,
            })
        # Price List / currency / exchange rate aren't set anywhere above —
        # normally the desk UI's client script fills these in as items are
        # added; built server-side like this, they're left blank and
        # insert() fails on mandatory fields without this.
        so.set_missing_values()
        so.insert(ignore_permissions=True)
        so.submit()

        self.sales_order = so.name
        self.status = "Accepted"
        self.accepted_at = frappe.utils.now_datetime()
        self.save(ignore_permissions=True)

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
    def mark_dispatched(self):
        if self.status not in ("Accepted", "Preparing"):
            frappe.throw(_(f"Cannot dispatch in status: {self.status}"))
        config = get_config()
        self.status = "Dispatched"
        self.dispatched_at = frappe.utils.now_datetime()
        self.save(ignore_permissions=True)
        enqueue_outbox(
            event_type="order.dispatched",
            payload={
                "order_id": self.hub_order_id,
                "vendor_id": config.vendor_id if config else "",
                "event_id": generate_event_id(),
                "event_seq": next_event_seq(),
            },
            voucher_type="Vendor Order", voucher_no=self.name,
        )

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
                so.cancel()
                # so.cancel() fires event_handlers.orders.on_sales_order_cancel,
                # which writes this same Vendor Order's status directly via
                # frappe.db.set_value() (needed for Sales Orders cancelled
                # outside this method too) — bumping `modified` underneath
                # this in-memory doc. Reload before continuing, or the
                # save() below hits a TimestampMismatchError.
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
    # real leaf node instead.
    cust.customer_group = (frappe.db.get_single_value("Selling Settings", "customer_group")
        or _first_leaf("Customer Group") or "Individual")
    cust.territory = (frappe.db.get_single_value("Selling Settings", "territory")
        or _first_leaf("Territory") or "Nepal")
    cust.insert(ignore_permissions=True)
    return cust.name

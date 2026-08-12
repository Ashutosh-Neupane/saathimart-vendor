import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt
from saathimart_vendor.utils import (
    get_config, hub_get, hub_post, enqueue_outbox,
    generate_event_id, next_event_seq, build_stock_payload, _get_base_qty,
)


class ProductMapping(Document):
    def before_validate(self):
        if not self.vendor and get_config():
            self.vendor = get_config().vendor_id

    def validate(self):
        if not self.barcode:
            frappe.throw(_("Barcode is required"))
        if not self.vendor:
            frappe.throw(_("Vendor is required"))
        if self.item_code and frappe.db.exists("Product Mapping", {
            "vendor": self.vendor,
            "item_code": self.item_code,
            "name": ["!=", self.name or ""]
        }):
            frappe.throw(_("Item {0} is already mapped for this vendor").format(self.item_code))
        if frappe.db.exists("Product Mapping", {
            "vendor": self.vendor,
            "barcode": self.barcode,
            "name": ["!=", self.name or ""]
        }):
            frappe.throw(_("Barcode {0} is already mapped to a different item for this vendor").format(self.barcode))

    @frappe.whitelist()
    def sync_with_hub(self):
        """Lookup barcode on hub and fill hub_product_id + hub_sku."""
        config = get_config()
        if not config:
            frappe.throw(_("Vendor Config not set up"))

        if not self.vendor:
            self.vendor = config.vendor_id

        result = hub_get(config, "saathimart.api.products.lookup_by_barcode",
                         {"barcode": self.barcode})
        if not result:
            self.sync_status = "Error"
            self.sync_error = f"Barcode {self.barcode} not found on hub"
            self.save(ignore_permissions=True)
            frappe.throw(_(f"Barcode {self.barcode} not found on SaathiMart hub"))

        self.hub_product_id = result.get("name")
        self.hub_sku = result.get("sku") or ""
        self.sync_status = "Mapped"
        self.sync_error = ""
        self.last_synced = frappe.utils.now_datetime()
        self.save(ignore_permissions=True)

        self._auto_create_vendor_listing(result)
        return {"hub_product_id": self.hub_product_id, "hub_sku": self.hub_sku}

    def _get_current_selling_price(self):
        """Current selling Item Price for this mapping's item_code, or 0 if none set."""
        if not self.item_code:
            return 0
        rate = frappe.db.get_value(
            "Item Price", {"item_code": self.item_code, "selling": 1}, "price_list_rate"
        )
        return flt(rate or 0)

    def _get_current_actual_qty(self, config):
        """Current Bin.actual_qty for this mapping's item_code in the vendor's default warehouse."""
        if not self.item_code or not config or not config.default_warehouse:
            return 0
        qty = frappe.db.get_value(
            "Bin", {"item_code": self.item_code, "warehouse": config.default_warehouse}, "actual_qty"
        )
        return flt(qty or 0)

    def _auto_create_vendor_listing(self, hub_result):
        """
        Create a Vendor Listing on the hub, seeded with whatever this
        mapping's item already has — not just a price=0/available_qty=0
        placeholder that sits unsellable until some *unrelated* later
        Item Price/stock edit happens to trigger a resync. A vendor mapping
        an item they've already been stocking and pricing for a while
        (the common case — mapping usually happens well after an item is
        already set up in their own ERPNext) shouldn't have to touch that
        item again just to make the connection hub-side actually sellable.
        """
        config = get_config()
        if not config or not self.hub_product_id:
            return

        current_price = self._get_current_selling_price()
        current_qty = self._get_current_actual_qty(config)

        try:
            ok, msg = hub_post(config, "saathimart.api.products.create_vendor_listing", {
                "product": self.hub_product_id,
                "vendor": self.vendor,
                "price": current_price,
                "compare_price": 0,
                "barcode": self.barcode,
                "sku": self.item_code,
                "status": "Active",
                "track_inventory": 1,
                "allow_backorder": 0,
                # Vendor Listing.available_qty is a display-only field — the
                # hub's actual stock/reservation logic reads Vendor Stock,
                # not this column (confirmed: products.py's listing enrich
                # step overwrites whatever's here from the Vendor Stock
                # join at read time). Still passed through for the Listing
                # row itself to be internally consistent, but the push
                # below is what actually creates a real Vendor Stock row.
                "available_qty": current_qty,
                "priority": 1,
                "estimated_delivery_minutes": 20,
            })
            if ok:
                frappe.logger().info(f"Auto-created Vendor Listing for {self.hub_product_id} / {self.vendor}")
        except Exception as e:
            frappe.logger().warning(f"Failed to auto-create Vendor Listing: {str(e)}")

        if current_qty > 0:
            enqueue_outbox(
                event_type="stock.receipt",
                payload={
                    **build_stock_payload(
                        self, qty_change=current_qty,
                        voucher_type="Product Mapping", voucher_no=self.name,
                        source_site="", remarks="Initial stock on mapping creation",
                        base_qty=_get_base_qty(self.item_code),
                    ),
                },
                voucher_type="Product Mapping", voucher_no=self.name,
            )

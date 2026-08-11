import frappe
from frappe import _
from frappe.model.document import Document
from saathimart_vendor.utils import get_config, hub_get, hub_post


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

    def _auto_create_vendor_listing(self, hub_result):
        """Create a Vendor Listing on the hub with default values."""
        config = get_config()
        if not config or not self.hub_product_id:
            return
        try:
            ok, msg = hub_post(config, "saathimart.api.products.create_vendor_listing", {
                "product": self.hub_product_id,
                "vendor": self.vendor,
                "price": 0,
                "compare_price": 0,
                "barcode": self.barcode,
                "sku": self.item_code,
                "status": "Active",
                "track_inventory": 1,
                "allow_backorder": 0,
                "available_qty": 0,
                "priority": 1,
                "estimated_delivery_minutes": 20,
            })
            if ok:
                frappe.logger().info(f"Auto-created Vendor Listing for {self.hub_product_id} / {self.vendor}")
        except Exception as e:
            frappe.logger().warning(f"Failed to auto-create Vendor Listing: {str(e)}")

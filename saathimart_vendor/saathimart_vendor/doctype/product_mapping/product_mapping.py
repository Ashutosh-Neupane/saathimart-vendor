import frappe
from frappe import _
from frappe.model.document import Document
from saathimart_vendor.utils import get_config, hub_get


class ProductMapping(Document):
    def validate(self):
        if not self.barcode:
            frappe.throw(_("Barcode is required"))

    @frappe.whitelist()
    def sync_with_hub(self):
        """Lookup barcode on hub and fill hub_product_id + hub_sku."""
        config = get_config()
        if not config:
            frappe.throw(_("Vendor Config not set up"))

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
        return {"hub_product_id": self.hub_product_id, "hub_sku": self.hub_sku}

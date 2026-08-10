import frappe
from frappe.model.document import Document


class VendorConfig(Document):
    def validate(self):
        if self.hub_url:
            self.hub_url = self.hub_url.rstrip("/")
        if not self.vendor_id:
            frappe.throw("Vendor ID is required — get this from your SaathiMart admin")

import frappe
from frappe.model.document import Document


class VendorWarehouse(Document):
    """A physical warehouse/location on the vendor's ERPNext site.

    Child table of Vendor Config. Synced from the hub's Vendor Warehouses.
    Maps each hub warehouse to a local ERPNext Warehouse for stock tracking
    and order fulfillment.
    """

    def validate(self):
        # Only one default warehouse per vendor config
        if self.is_default and self.parent and self.parenttype == "Vendor Config":
            existing = [
                r.name
                for r in frappe.get_all(
                    "Vendor Warehouse",
                    filters={
                        "parent": self.parent,
                        "parenttype": "Vendor Config",
                        "is_default": 1,
                        "name": ("!=", self.name),
                    },
                )
            ]
            if existing:
                frappe.throw(
                    "Already have a default warehouse ({0}). Uncheck it first.".format(
                        existing[0]
                    )
                )

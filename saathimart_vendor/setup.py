"""Post-migrate setup for saathimart_vendor.

Custom fields the receiver relies on (see hooks.py after_migrate):
  Journal Entry.sm_hub_ref — idempotency key for the hub's
  platform.ledger_entry batches; re-deliveries with the same hub
  reference are skipped instead of double-booking the platform's books.
"""
import frappe


def ensure_custom_fields():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields({
		"Journal Entry": [
			dict(
				fieldname="sm_hub_ref",
				label="SaathiMart Hub Reference",
				fieldtype="Data",
				insert_after="user_remark",
				read_only=1,
				print_hide=1,
				allow_on_submit=1,
				description="Idempotency key from the SaathiMart hub for platform.ledger_entry pushes",
			),
		],
	})

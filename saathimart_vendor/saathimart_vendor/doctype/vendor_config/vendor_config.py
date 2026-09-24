import frappe
from frappe.model.document import Document


class VendorConfig(Document):
	_SECRET_FIELDS = ("webhook_secret", "webhook_secret_old", "webhook_secret_next")

	def validate(self):
		if self.hub_url:
			self.hub_url = self.hub_url.rstrip("/")
		if not self.vendor_id:
			frappe.throw("Vendor ID is required — get this from your SaathiMart admin")

		# Frappe's _save_passwords() deletes a Password field's __Auth row
		# whenever its in-memory value is empty — and Password fields always
		# load as None — so ANY plain .save() of this single (boot config,
		# desk edit, rotation stage) silently wiped every stored secret and
		# broke hub↔vendor authentication until the next registration
		# re-stored them. Secrets are only ever written through
		# set_encrypted_password() or an explicitly-set field value, so
		# preserve any field the current save doesn't carry a value for.
		ignore = self.flags.get("ignore_save_passwords")
		if ignore is not True:  # True already skips every password field
			preserve = [
				f for f in self._SECRET_FIELDS
				if not self.get(f)
			]
			self.flags.ignore_save_passwords = list(ignore or []) + preserve

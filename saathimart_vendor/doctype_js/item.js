frappe.ui.form.on("Item", {
	refresh(frm) {
		// One-click "Sync to Saathi" — pushes this item to the marketplace:
		// Product + Vendor Listing (with our price) + stock, all in one call.
		// Only meaningful on vendor sites running the saathimart_vendor app
		// with sync enabled; the server endpoint guards the rest.
		frm.add_custom_button(__("Sync to Saathi"), () => {
			frappe.call({
				method: "saathimart_vendor.api.item_sync.sync_item_to_saathi",
				args: { item_code: frm.doc.name },
				freeze: true,
				freeze_message: __("Syncing to SaathiMart…"),
			}).then((r) => {
				const d = r.message || {};
				if (d.ok) {
					frappe.show_alert({
						message: __(d.message || "Synced to SaathiMart"),
						indicator: "green",
					});
					if (d.product_url) {
						frm.dashboard.add_comment(
							__("Synced to SaathiMart: {0} [open on hub]({1})").format(
								d.hub_product, d.product_url
							),
							"blue", true
						);
					}
				} else {
					frappe.msgprint(__("Sync did not complete — check the response."));
				}
			});
		}, __("SaathiMart"));

		// Gentle hint when the item can't be synced yet (no barcode).
		const has_barcode = (frm.doc.barcodes || []).some((b) => b.barcode);
		if (!has_barcode && !frm.is_new()) {
			frm.set_intro(__(
				"This item has no barcode. SaathiMart matches products by barcode — "
				+ "add one under Barcodes before using 'Sync to Saathi'."
			), "orange");
		}
	},
});

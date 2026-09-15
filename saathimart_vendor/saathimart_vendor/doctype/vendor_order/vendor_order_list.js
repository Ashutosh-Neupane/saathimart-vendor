frappe.listview_settings["Vendor Order"] = {
	add_fields: ["status", "hub_order_id"],
	get_indicator(doc) {
		if (doc.status === "Delivered") {
			return [__("Delivered"), "green", "status,=,Delivered"];
		} else if (doc.status === "Dispatched") {
			return [__("Dispatched"), "blue", "status,=,Dispatched"];
		} else if (doc.status === "Preparing") {
			return [__("Preparing"), "orange", "status,=,Preparing"];
		} else if (doc.status === "Accepted") {
			return [__("Accepted"), "blue", "status,=,Accepted"];
		} else if (doc.status === "Received") {
			return [__("Received"), "yellow", "status,=,Received"];
		} else if (doc.status === "Cancelled") {
			return [__("Cancelled"), "gray", "status,=,Cancelled"];
		}
	},
};

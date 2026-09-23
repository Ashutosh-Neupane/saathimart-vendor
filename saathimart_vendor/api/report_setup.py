"""Idempotent installer for the vendor app's Query Reports.

The vendor app ships inside a read-only image on some deployments, so
file-based Script Reports can't be counted on. Query Reports live in the
DB — created/upserted here per site, callable from any deployment:

    bench --site vendor1.localhost execute \
        saathimart_vendor.api.report_setup.install_vendor_reports

Also runs automatically during `bench migrate` (see hooks.py) so a fresh
site always has the reports.
"""
import frappe

REPORTS = {
    "Vendor Sales by Product": {
        "ref_doctype": "Vendor Order",
        "sql": """
            SELECT
                voi.item_code AS item_code,
                COUNT(DISTINCT vo.name) AS orders,
                SUM(voi.qty) AS units_sold,
                SUM(voi.amount) AS revenue,
                SUM(CASE WHEN voi.qty >= 2 THEN 1 ELSE 0 END) AS multi_qty_lines
            FROM `tabVendor Order Item` voi
            JOIN `tabVendor Order` vo ON vo.name = voi.parent
            WHERE vo.docstatus < 2
              AND DATE(vo.received_at) BETWEEN %(from_date)s AND %(to_date)s
            GROUP BY voi.item_code
            ORDER BY revenue DESC
        """,
        "columns": [
            {"label": "Item", "fieldname": "item_code", "fieldtype": "Data", "width": 260},
            {"label": "Orders", "fieldname": "orders", "fieldtype": "Int", "width": 90},
            {"label": "Units Sold", "fieldname": "units_sold", "fieldtype": "Int", "width": 110},
            {"label": "Revenue", "fieldname": "revenue", "fieldtype": "Currency", "width": 150},
            {"label": "Multi-qty Lines", "fieldname": "multi_qty_lines", "fieldtype": "Int", "width": 130},
        ],
    },
    "Vendor Payment Status": {
        "ref_doctype": "Vendor Order",
        "sql": """
            SELECT
                COALESCE(vo.payment_method, 'Unknown') AS payment_method,
                COUNT(*) AS orders,
                SUM(vo.grand_total) AS total_amount,
                SUM(CASE WHEN vo.payment_status = 'Paid' THEN vo.grand_total ELSE 0 END) AS collected,
                SUM(CASE WHEN vo.payment_status != 'Paid' THEN vo.grand_total ELSE 0 END) AS outstanding
            FROM `tabVendor Order` vo
            WHERE vo.docstatus < 2
              AND DATE(vo.received_at) BETWEEN %(from_date)s AND %(to_date)s
            GROUP BY vo.payment_method
            ORDER BY total_amount DESC
        """,
        "columns": [
            {"label": "Payment Method", "fieldname": "payment_method", "fieldtype": "Data", "width": 180},
            {"label": "Orders", "fieldname": "orders", "fieldtype": "Int", "width": 90},
            {"label": "Total Amount", "fieldname": "total_amount", "fieldtype": "Currency", "width": 150},
            {"label": "Collected", "fieldname": "collected", "fieldtype": "Currency", "width": 150},
            {"label": "Outstanding", "fieldname": "outstanding", "fieldtype": "Currency", "width": 150},
        ],
    },
    "Sync Outbox Health": {
        "ref_doctype": "Sync Outbox",
        "sql": """
            SELECT
                event_type,
                status,
                COUNT(*) AS events,
                SUM(CASE WHEN retry_count > 0 THEN 1 ELSE 0 END) AS retried,
                MAX(retry_count) AS max_retries,
                MAX(last_error) AS last_error
            FROM `tabSync Outbox`
            GROUP BY event_type, status
            ORDER BY event_type, status
        """,
        "columns": [
            {"label": "Event Type", "fieldname": "event_type", "fieldtype": "Data", "width": 220},
            {"label": "Status", "fieldname": "status", "fieldtype": "Data", "width": 110},
            {"label": "Events", "fieldname": "events", "fieldtype": "Int", "width": 90},
            {"label": "Retried", "fieldname": "retried", "fieldtype": "Int", "width": 90},
            {"label": "Max Retries", "fieldname": "max_retries", "fieldtype": "Int", "width": 110},
            {"label": "Last Error", "fieldname": "last_error", "fieldtype": "Data", "width": 340},
        ],
    },
}


def install_vendor_reports():
    """Upsert every vendor Query Report. Returns a summary dict."""
    created, updated = [], []
    for name, spec in REPORTS.items():
        row = frappe.db.get_value("Report", name, ["name", "query"], as_dict=True)
        sql = spec["sql"]
        if row:
            if (row.query or "").strip() != sql.strip():
                frappe.db.set_value("Report", row.name, "query", sql)
                updated.append(name)
        else:
            doc = frappe.new_doc("Report")
            doc.report_name = name
            doc.ref_doctype = spec["ref_doctype"]
            doc.report_type = "Query Report"
            doc.is_standard = "No"
            doc.query = sql
            doc.extend("columns", spec["columns"])
            # Vendor sites only carry System Manager (no SM Vendor role on
            # this side of the split) — guard against missing roles.
            existing_roles = set(frappe.get_all("Role", pluck="role_name"))
            for role in ("System Manager", "SM Vendor"):
                if role in existing_roles:
                    doc.append("roles", {"role": role})
            doc.insert(ignore_permissions=True)
            created.append(name)
    frappe.db.commit()
    return {"created": created, "updated": updated, "total": len(REPORTS)}

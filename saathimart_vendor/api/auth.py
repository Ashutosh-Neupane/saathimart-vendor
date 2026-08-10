import frappe


def has_permission():
    roles = frappe.get_roles()
    return "System Manager" in roles or "Sales User" in roles

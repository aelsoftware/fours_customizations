import frappe


def execute():
	"""Set allow_on_submit = 1 on the custom_sales_invoice field on Sales Order
	so it can be cleared after the SO is submitted (e.g. on DN deletion)."""
	frappe.db.set_value(
		"Custom Field",
		{"dt": "Sales Order", "fieldname": "custom_sales_invoice"},
		"allow_on_submit",
		1,
		update_modified=False,
	)
	frappe.db.commit()
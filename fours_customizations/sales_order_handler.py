import frappe


def on_cancel(doc, method=None):
	"""When a Sales Order is cancelled, clear the custom_sales_invoice back-link."""
	if not doc.custom_sales_invoice:
		return

	frappe.db.set_value(
		"Sales Order",
		doc.name,
		"custom_sales_invoice",
		None,
		update_modified=False,
	)
import frappe


def before_cancel(doc, method=None):
	"""Clear the custom_sales_invoice back-link and suppress link validation
	before ERPNext's validator runs."""
	frappe.db.set_value(
		"Sales Order",
		doc.name,
		"custom_sales_invoice",
		None,
		update_modified=False,
	)
	doc.flags.ignore_links = True


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
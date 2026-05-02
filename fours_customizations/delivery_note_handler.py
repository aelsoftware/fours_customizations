import frappe


def on_trash(doc, method=None):
	"""When a Delivery Note is deleted, cancel linked Sales Invoices."""
	sales_invoices = _get_linked_sales_invoices(doc)

	for si_name in sales_invoices:
		si = frappe.get_doc("Sales Invoice", si_name)

		if si.docstatus != 1:
			continue

		if not frappe.db.get_value("Company", si.company, "enable_selling_automations"):
			continue

		si.cancel()
		frappe.msgprint(f"Cancelled Sales Invoice {si_name}.", alert=True)


def _get_linked_sales_invoices(doc):
	"""Get unique Sales Invoice names linked from DN items."""
	invoices = set()
	for item in doc.items:
		if item.against_sales_invoice:
			invoices.add(item.against_sales_invoice)
	return invoices

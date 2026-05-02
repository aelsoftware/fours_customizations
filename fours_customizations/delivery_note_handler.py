import frappe


def on_trash(doc, method=None):
	"""When a Delivery Note is deleted, cancel linked Sales Orders and Sales Invoices."""
	sales_invoices = _get_linked_sales_invoices(doc)
	sales_orders = _get_linked_sales_orders(doc)

	# Cancel SOs first — before the SI cancel touches SO status
	for so_name in sales_orders:
		so = frappe.get_doc("Sales Order", so_name)

		if so.docstatus != 1:
			continue

		if not frappe.db.get_value("Company", so.company, "enable_selling_automations"):
			continue

		so.flags.ignore_permissions = True
		so.cancel()
		frappe.msgprint(f"Cancelled Sales Order {so_name}.", alert=True)

	for si_name in sales_invoices:
		si = frappe.get_doc("Sales Invoice", si_name)

		if si.docstatus != 1:
			continue

		if not frappe.db.get_value("Company", si.company, "enable_selling_automations"):
			continue

		si.flags.ignore_permissions = True
		si.flags.ignore_links = True
		si.cancel()
		frappe.msgprint(f"Cancelled Sales Invoice {si_name}.", alert=True)


def _get_linked_sales_invoices(doc):
	"""Get unique Sales Invoice names linked from DN items."""
	invoices = set()
	for item in doc.items:
		if item.against_sales_invoice:
			invoices.add(item.against_sales_invoice)
	return invoices


def _get_linked_sales_orders(doc):
	"""Get unique Sales Order names linked from DN items."""
	orders = set()
	for item in doc.items:
		if item.against_sales_order:
			orders.add(item.against_sales_order)
	return orders
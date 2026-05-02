import frappe


def on_trash(doc, method=None):
	"""When a Delivery Note is deleted, cancel linked Sales Orders and Sales Invoices."""
	sales_invoices = _get_linked_sales_invoices(doc)
	sales_orders = _get_linked_sales_orders(doc)

	# Clear dn_detail and delivery_note on all SI item rows that point to this DN
	# so ERPNext's link validation doesn't block the delete.
	_unlink_si_items(doc)

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


def _unlink_si_items(doc):
	"""Clear dn_detail and delivery_note on SI item rows that point to this DN,
	so ERPNext's link validation does not block the DN deletion."""
	dn_item_names = [item.name for item in doc.items if item.name]
	if not dn_item_names:
		return

	# Find SI items whose dn_detail points to one of this DN's item rows
	si_items = frappe.get_all(
		"Sales Invoice Item",
		filters={"dn_detail": ["in", dn_item_names]},
		pluck="name",
	)

	for si_item_name in si_items:
		frappe.db.set_value(
			"Sales Invoice Item",
			si_item_name,
			{
				"dn_detail": None,
				"delivery_note": None,
			},
			update_modified=False,
		)
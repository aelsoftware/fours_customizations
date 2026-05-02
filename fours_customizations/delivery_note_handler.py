import frappe


def on_trash(doc, method=None):
	"""When a Delivery Note is deleted, cancel linked Sales Invoices and Sales Orders."""
	sales_invoices = _get_linked_sales_invoices(doc)
	sales_orders = _get_linked_sales_orders(doc)

	# Unlink all back-references to this DN so ERPNext's link validation
	# does not block the delete.
	_unlink_si_items(doc)
	_unlink_so_items(doc)

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

	for so_name in sales_orders:
		so = frappe.get_doc("Sales Order", so_name)

		if so.docstatus != 1:
			continue

		if not frappe.db.get_value("Company", so.company, "enable_selling_automations"):
			continue

		so.flags.ignore_permissions = True
		so.flags.ignore_links = True
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
	"""Clear dn_detail and delivery_note on SI item rows pointing to this DN."""
	dn_item_names = [item.name for item in doc.items if item.name]
	if not dn_item_names:
		return

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


def _unlink_so_items(doc):
	"""Clear against_sales_order and so_detail on DN item rows, and clear
	delivery_note references on SO item rows pointing to this DN."""
	dn_item_names = [item.name for item in doc.items if item.name]
	so_names = _get_linked_sales_orders(doc)

	# Clear the SO link fields on DN items themselves so the SO cancel
	# does not see any fulfilled quantity tied to this DN.
	for dn_item in doc.items:
		frappe.db.set_value(
			"Delivery Note Item",
			dn_item.name,
			{
				"against_sales_order": None,
				"so_detail": None,
			},
			update_modified=False,
		)

	if not so_names:
		return

	# Clear any SO item rows that reference this DN
	so_items = frappe.get_all(
		"Sales Order Item",
		filters={
			"parent": ["in", list(so_names)],
			"delivery_note": doc.name,
		},
		pluck="name",
	)

	for so_item_name in so_items:
		frappe.db.set_value(
			"Sales Order Item",
			so_item_name,
			"delivery_note",
			None,
			update_modified=False,
		)
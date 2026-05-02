import frappe


def before_cancel(doc, method=None):
	"""Unlink all back-references and suppress link validation on cancel."""
	_unlink_si_items(doc)
	_unlink_so_items(doc)
	# Suppress ERPNext's link validator entirely — we have already cleared all
	# back-references above, but the validator also checks the reverse direction
	# (fields on Delivery Note that point to Sales Invoice / Sales Order) which
	# we cannot clear without amending a submitted document. ignore_links bypasses
	# that check so the cancel can proceed.
	doc.flags.ignore_links = True


def on_cancel(doc, method=None):
	# """When a Delivery Note is cancelled, unlink all back-references."""
	_unlink_si_items(doc)
	_unlink_so_items(doc)


def on_trash(doc, method=None):
	"""When a Delivery Note is deleted, unlink back-references then cancel
	linked Sales Invoices and Sales Orders."""
	_unlink_si_items(doc)
	_unlink_so_items(doc)

	sales_invoices = _get_linked_sales_invoices(doc)
	sales_orders = _get_linked_sales_orders(doc)

	for si_name in sales_invoices:
		si = frappe.get_doc("Sales Invoice", si_name)

		if si.docstatus != 1:
			continue

		if not frappe.db.get_value("Company", si.company, "enable_selling_automations"):
			continue

		# Clear the back-link before cancelling so ERPNext's link validation
		# on the SO side does not block the SI cancel.
		frappe.db.set_value("Sales Invoice", si_name, "custom_sales_invoice", None, update_modified=False)

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

		# Clear the back-link before cancelling so no document points back
		# to this SO during the cancel validation.
		frappe.db.set_value("Sales Order", so_name, "custom_sales_invoice", None, update_modified=False)

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
    if doc.docstatus != 1:
        return
    
    """Clear dn_detail and delivery_note on SI item rows pointing to this DN.

	Two passes are needed because ERPNext checks both fields independently:
	  - dn_detail  : the DN item row name (child row link)
	  - delivery_note : the DN document name (header link)
	Either one present is enough for the link validator to block the delete.
	"""
    dn_item_names = [item.name for item in doc.items if item.name]

	# Pass 1: rows matched by dn_detail (DN item row name)
    if dn_item_names:
        si_items_by_detail = frappe.get_all(
			"Sales Invoice Item",
			filters={"dn_detail": ["in", dn_item_names]},
			pluck="name",
		)
        for si_item_name in si_items_by_detail:
            frappe.db.set_value(
				"Sales Invoice Item",
				si_item_name,
				{"dn_detail": None, "delivery_note": None},
				update_modified=False,
			)

	# Pass 2: rows matched by delivery_note (DN document name) — catches any
	# SI items that reference this DN header but whose dn_detail differs
	# (e.g. manually linked rows, or rows from a previous partial delivery).
    si_items_by_dn = frappe.get_all(
		"Sales Invoice Item",
		filters={"delivery_note": doc.name},
		pluck="name",
	)
    for si_item_name in si_items_by_dn:
        frappe.db.set_value(
			"Sales Invoice Item",
			si_item_name,
			{"dn_detail": None, "delivery_note": None},
			update_modified=False,
		)


def _unlink_so_items(doc):
	if doc.docstatus != 1:
		return

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
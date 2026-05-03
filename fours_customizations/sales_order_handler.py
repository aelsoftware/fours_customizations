import frappe


def before_cancel(doc, method=None):
    doc.flags.ignore_links = True
    doc.flags.ignore_validate = True
    
    """When a Sales Order is cancelled, clear the custom_sales_invoice back-link."""
    if not doc.custom_sales_invoice:
        return

    # frappe.db.set_value(
	# 	"Sales Order",
	# 	doc.name,
	# 	"custom_sales_invoice",
	# 	None,
	# 	update_modified=False,
	# )
 
    # """Clear all back-references to this SO and suppress link validation
	# before ERPNext's validator runs."""
    # _cancel_stock_reservations(doc)
    # _unlink_si_items(doc)
    # _unlink_dn_items(doc)
    # frappe.db.set_value(
	# 	"Sales Order",
	# 	doc.name,
	# 	"custom_sales_invoice",
	# 	None,
	# 	update_modified=False,
	# )



def on_cancel(doc, method=None):
    if doc.docstatus != 1:
        return
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


def _cancel_stock_reservations(doc):
	"""Cancel any active Stock Reservation Entries for this SO so ERPNext
	does not block the SO cancel due to reserved stock."""
	if not frappe.db.table_exists("Stock Reservation Entry"):
		return

	sre_names = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"voucher_type": "Sales Order",
			"voucher_no": doc.name,
			"docstatus": 1,
		},
		pluck="name",
	)

	for sre_name in sre_names:
		sre = frappe.get_doc("Stock Reservation Entry", sre_name)
		sre.flags.ignore_permissions = True
		sre.flags.ignore_links = True
		sre.cancel()


def _unlink_si_items(doc):
	if doc.docstatus != 1:
		return

	"""Clear all SO/DN back-references on SI header and item rows linked to this SO."""
	so_item_names = [item.name for item in doc.items if item.name]

	if not so_item_names:
		return

	# ── SI item rows linked via so_detail ─────────────────────────────────────
	si_items = frappe.get_all(
		"Sales Invoice Item",
		filters={"so_detail": ["in", so_item_names]},
		pluck="name",
	)
	for si_item_name in si_items:
		frappe.db.set_value(
			"Sales Invoice Item",
			si_item_name,
			{
				"sales_order": None,
				"so_detail": None,
				"dn_detail": None,
				"delivery_note": None,
			},
			update_modified=False,
		)

	# ── SI item rows linked via sales_order name directly ────────────────────
	si_items_by_so = frappe.get_all(
		"Sales Invoice Item",
		filters={"sales_order": doc.name},
		pluck="name",
	)
	for si_item_name in si_items_by_so:
		frappe.db.set_value(
			"Sales Invoice Item",
			si_item_name,
			{
				"sales_order": None,
				"so_detail": None,
				"dn_detail": None,
				"delivery_note": None,
			},
			update_modified=False,
		)


def _unlink_dn_items(doc):
	if doc.docstatus != 1:
		return

	"""Clear against_sales_order and so_detail on DN item rows linked to this SO,
	and clear the SI↔DN links on any SI items those DN items point to."""
	so_item_names = [item.name for item in doc.items if item.name]

	# Find DN items linked to this SO
	dn_item_filters = [
		{"so_detail": ["in", so_item_names]},
		{"against_sales_order": doc.name},
	]

	dn_item_names = set()
	dn_names = set()

	for f in dn_item_filters:
		rows = frappe.get_all(
			"Delivery Note Item",
			filters=f,
			fields=["name", "parent"],
		)
		for row in rows:
			dn_item_names.add(row.name)
			dn_names.add(row.parent)

	# Clear SO link fields on DN items
	for dn_item_name in dn_item_names:
		frappe.db.set_value(
			"Delivery Note Item",
			dn_item_name,
			{
				"against_sales_order": None,
				"so_detail": None,
			},
			update_modified=False,
		)

	if not dn_item_names:
		return

	# Clear SI item rows that reference any of these DN items
	si_items_by_dn_detail = frappe.get_all(
		"Sales Invoice Item",
		filters={"dn_detail": ["in", list(dn_item_names)]},
		pluck="name",
	)
	for si_item_name in si_items_by_dn_detail:
		frappe.db.set_value(
			"Sales Invoice Item",
			si_item_name,
			{"dn_detail": None, "delivery_note": None},
			update_modified=False,
		)

	# Also clear by delivery_note header name
	if dn_names:
		si_items_by_dn = frappe.get_all(
			"Sales Invoice Item",
			filters={"delivery_note": ["in", list(dn_names)]},
			pluck="name",
		)
		for si_item_name in si_items_by_dn:
			frappe.db.set_value(
				"Sales Invoice Item",
				si_item_name,
				{"dn_detail": None, "delivery_note": None},
				update_modified=False,
			)
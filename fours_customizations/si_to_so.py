"""
si_to_so.py — Auto-create + submit a Sales Order when a Sales Invoice is
submitted (Req #1).

The Sales Order is used purely as a stock-reservation mechanism: it gathers
the same items the SI carries, lets ERPNext fan out Stock Reservation Entries
against the warehouse, and is then linked back to the SI.  We do NOT amend
existing flows that already create Delivery Notes — this just gives stock
control a paper-trail.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_setting,
)


def _enabled() -> bool:
	return bool(get_setting("enable_so_from_si", 1))


def _default_warehouse(company: str) -> str | None:
	w = get_setting("default_so_warehouse") or get_setting("default_warehouse")
	if w:
		return w
	# fall back to the company's default warehouse if any
	return frappe.db.get_value("Company", company, "default_warehouse")


def create_sales_order_for_invoice(si) -> str | None:
	"""Create and submit a Sales Order mirroring `si`. Returns the SO name.

	Returns None if the feature is disabled, the SI is a return, or if an SO
	has already been linked to this invoice.
	"""
	if not _enabled():
		return None
	if si.is_return:
		return None
	if getattr(si, "custom_auto_created_sales_order", None):
		return si.custom_auto_created_sales_order

	# Don't create if all items are non-stock — there's nothing to reserve.
	stock_items = {
		row[0]
		for row in frappe.get_all(
			"Item",
			filters={
				"name": ("in", [item.item_code for item in si.items if item.item_code]),
				"is_stock_item": 1,
			},
			fields=["name"],
			as_list=True,
		)
	}
	if not stock_items:
		return None

	warehouse = _default_warehouse(si.company)
	delivery_date = getdate(si.due_date) if si.due_date else add_days(getdate(si.posting_date), 1)

	so = frappe.new_doc("Sales Order")
	so.customer = si.customer
	so.customer_name = si.customer_name
	so.company = si.company
	so.currency = si.currency
	so.conversion_rate = si.conversion_rate
	so.transaction_date = si.posting_date or nowdate()
	so.delivery_date = delivery_date
	so.selling_price_list = si.selling_price_list
	so.price_list_currency = si.price_list_currency
	so.plc_conversion_rate = si.plc_conversion_rate
	so.set_warehouse = si.set_warehouse or warehouse
	so.cost_center = si.cost_center or frappe.get_cached_value("Company", si.company, "cost_center")
	so.taxes_and_charges = si.taxes_and_charges
	so.tax_category = si.tax_category
	so.sales_partner = si.sales_partner
	so.commission_rate = flt(si.commission_rate)
	so.total_commission = flt(si.total_commission)
	so.letter_head = si.letter_head
	so.custom_source_sales_invoice = si.name
	so.reserve_stock = 1

	# Keep Sales Invoice Item names aligned with the Sales Order Items we append,
	# so the native sales_order / so_detail links can be wired after insert.
	si_item_names: list[str] = []
	for item in si.items:
		if item.item_code not in stock_items:
			continue
		so.append("items", {
			"item_code": item.item_code,
			"item_name": item.item_name,
			"description": item.description,
			"qty": flt(item.qty),
			"uom": item.uom,
			"stock_uom": item.stock_uom,
			"conversion_factor": flt(item.conversion_factor) or 1,
			"rate": flt(item.rate),
			"price_list_rate": flt(item.price_list_rate),
			"discount_percentage": flt(item.discount_percentage),
			"discount_amount": flt(item.discount_amount),
			"warehouse": item.warehouse or so.set_warehouse,
			"cost_center": item.cost_center or so.cost_center,
			"delivery_date": delivery_date,
			"reserve_stock": 1,
		})
		si_item_names.append(item.name)

	if not so.items:
		return None

	for tax in si.taxes or []:
		so.append("taxes", {
			"charge_type": tax.charge_type,
			"account_head": tax.account_head,
			"cost_center": tax.cost_center,
			"description": tax.description,
			"rate": flt(tax.rate),
			"tax_amount": flt(tax.tax_amount),
			"included_in_print_rate": tax.included_in_print_rate,
			"row_id": tax.row_id,
		})

	so.flags.ignore_permissions = True
	so.flags.ignore_links = True
	# Skip the customer-debt block — the SI was already approved.
	so.flags.ignore_validate = True
	# The invoice flow already owns the (single) Delivery Note mapped off the SI,
	# so tell the Sales Order handler not to create a duplicate one on submit.
	so.flags.skip_auto_delivery_note = True
	try:
		so.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S SI->SO: insert failed")
		return None

	# Wire native ERPNext links across SI ↔ SO ↔ DN now that the Sales Order
	# Items have names. Done before submit so the links exist even if submit
	# fails and the SO is left as a draft for review.
	_apply_native_links(si, so, si_item_names)

	try:
		so.submit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"4S SI->SO: submit failed for {so.name}")
		# Leave the SO as draft so the user can review
		return so.name

	# Legacy convenience pointer on the invoice header. The authoritative links
	# are the native sales_order / so_detail fields set in _apply_native_links;
	# this is kept only for backward compatibility.
	try:
		frappe.db.set_value("Sales Invoice", si.name, "custom_auto_created_sales_order", so.name)
	except Exception:
		pass

	frappe.msgprint(
		f"Sales Order {so.name} created and submitted for stock reservation.",
		alert=True,
	)
	return so.name


def _apply_native_links(si, so, si_item_names: list[str]) -> None:
	"""Interlink the Sales Invoice, auto-created Sales Order, and draft Delivery
	Note through ERPNext's standard fields — no custom fields involved:

	  • Sales Invoice Item  → Sales Order   (sales_order, so_detail)
	  • Delivery Note Item  → Sales Order   (against_sales_order, so_detail)

	The Delivery Note already carries against_sales_invoice / si_detail from when
	it was mapped off the invoice, so after this runs all three documents are
	mutually discoverable in the standard "Connections" view and the Delivery
	Note's submission/deletion correctly flows back to the Sales Order.
	"""
	# Pair each Sales Invoice Item with its freshly-named Sales Order Item.
	# so.items and si_item_names are appended in lock-step, so they align by index.
	si_to_so_item = {
		si_item_name: so_item.name
		for si_item_name, so_item in zip(si_item_names, so.items)
		if si_item_name
	}
	if not si_to_so_item:
		return

	# 1. Sales Invoice Item → Sales Order. update_modified=False keeps the just
	#    -submitted invoice's timestamp stable (avoids a mid-submit clash).
	for si_item_name, so_item_name in si_to_so_item.items():
		frappe.db.set_value(
			"Sales Invoice Item",
			si_item_name,
			{"sales_order": so.name, "so_detail": so_item_name},
			update_modified=False,
		)

	# 2. Delivery Note Item → Sales Order, matched back through si_detail.
	dn_items = frappe.get_all(
		"Delivery Note Item",
		filters={
			"against_sales_invoice": si.name,
			"docstatus": 0,
			"si_detail": ["in", list(si_to_so_item)],
		},
		fields=["name", "si_detail"],
	)
	for row in dn_items:
		so_item_name = si_to_so_item.get(row.si_detail)
		if so_item_name:
			frappe.db.set_value(
				"Delivery Note Item",
				row.name,
				{"against_sales_order": so.name, "so_detail": so_item_name},
				update_modified=False,
			)

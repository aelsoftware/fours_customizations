"""
si_to_so.py — Auto-create + submit a Sales Order for a Sales Invoice (Req #1).

Called from the invoice's ``before_submit`` so the Sales Order exists and is
linked *before* the invoice's billing pass runs. The invoice items are wired to
the order through the native ``sales_order`` / ``so_detail`` fields and
``update_billed_amount_in_sales_order`` is turned on, so ERPNext rolls the
billed amount into the Sales Order and the SO / DN cancel cleanly in reverse
order. The order also reserves stock against the warehouse (best-effort, after
submit). The draft Delivery Note, mapped off the invoice in ``on_submit``,
inherits ``against_sales_order`` / ``so_detail`` automatically.
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
	# Idempotency guard: skip if a Sales Order has already been spun up for this
	# invoice. The auto-SO carries custom_source_sales_invoice back to the SI, so
	# that is the reliable key — no pointer field on the invoice needed.
	existing_so = frappe.db.get_value(
		"Sales Order", {"custom_source_sales_invoice": si.name}, "name"
	)
	if existing_so:
		return existing_so

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
	posting = getdate(si.posting_date) if si.posting_date else getdate(nowdate())
	delivery_date = getdate(si.due_date) if si.due_date else add_days(posting, 1)
	# The order now runs full validate(), which rejects a delivery date before the
	# transaction date — clamp it (e.g. an overdue invoice billed after due_date).
	if delivery_date < posting:
		delivery_date = posting

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
	# Mirror the invoice's rates exactly — don't let pricing rules re-rate the
	# order, or its total (and the billed-amount rollup) would drift from the SI.
	so.ignore_pricing_rule = 1
	# Reserve stock AFTER submit (see _reserve_stock_best_effort below). Auto
	# -reserving during submit crashes on some ERPNext builds for hand-built
	# orders ('SalesOrderItem' has no attribute 'parent_detail_docname'), so we
	# keep reservation off the critical submit path.
	so.reserve_stock = 0

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
			# Turned on post-submit by _reserve_stock_best_effort.
			"reserve_stock": 0,
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
	# Skip the customer-debt block (the invoice was already approved) — but do NOT
	# use ignore_validate: that skips calculate_taxes_and_totals, leaving the order
	# with zero amounts and breaking the billed-amount rollup.
	so.flags.skip_debt_check = True
	# The invoice flow already owns the (single) Delivery Note mapped off the SI,
	# so tell the Sales Order handler not to create a duplicate one on submit.
	so.flags.skip_auto_delivery_note = True
	try:
		so.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S SI->SO: insert failed")
		return None

	try:
		so.submit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"4S SI->SO: submit failed for {so.name}")
		# Leave the SO as a draft for review. Don't link the invoice to an
		# unsubmitted order — that would break the billing rollup on submit.
		return so.name

	# Order is submitted: link the invoice items to it natively and switch on
	# billed-amount rollup. We run inside the invoice's before_submit, so these
	# in-memory writes persist with the invoice and ERPNext updates the Sales
	# Order's billed amount during submit. The draft Delivery Note then inherits
	# against_sales_order / so_detail when it is mapped off the invoice.
	_link_invoice_to_sales_order(si, so, si_item_names)

	# Reserve stock now that the order is safely submitted — best-effort only.
	_reserve_stock_best_effort(so)

	frappe.msgprint(
		f"Sales Order {so.name} created and submitted for stock reservation.",
		alert=True,
	)
	return so.name


def _link_invoice_to_sales_order(si, so, si_item_names: list[str]) -> None:
	"""Link the in-memory Sales Invoice items to the just-submitted Sales Order
	via the native ``sales_order`` / ``so_detail`` fields — no custom fields.

	Runs inside the invoice's before_submit, so these in-memory writes persist
	when the invoice submits and ERPNext rolls the billed amount into the Sales
	Order (``update_billed_amount_in_sales_order`` is turned on here). The draft
	Delivery Note inherits ``against_sales_order`` / ``so_detail`` automatically
	when it is mapped off the invoice in on_submit, so all three documents end up
	mutually linked and cancel cleanly in reverse order.
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

	for item in si.items:
		so_item_name = si_to_so_item.get(item.name)
		if so_item_name:
			item.sales_order = so.name
			item.so_detail = so_item_name

	# Let ERPNext update the Sales Order's billed amount when this invoice submits.
	si.update_billed_amount_in_sales_order = 1


def _reserve_stock_best_effort(so) -> None:
	"""Reserve stock for a freshly-submitted Sales Order without ever raising.

	The order is submitted with reservation turned off so submit can't crash;
	here we flip ``reserve_stock`` back on and let ERPNext create the Stock
	Reservation Entries. Any failure (including the build-specific
	'parent_detail_docname' error) is logged and swallowed — the submitted,
	interlinked Sales Order is the part that matters; reservation is a bonus.
	"""
	try:
		frappe.db.set_value("Sales Order", so.name, "reserve_stock", 1, update_modified=False)
		for item in so.items:
			if item.item_code:
				frappe.db.set_value(
					"Sales Order Item", item.name, "reserve_stock", 1, update_modified=False
				)
		so.reload()

		if hasattr(so, "create_stock_reservation_entries"):
			so.create_stock_reservation_entries()
		else:
			from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
				create_stock_reservation_entries_for_so_items,
			)
			create_stock_reservation_entries_for_so_items(so)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"4S SI->SO: post-submit stock reservation skipped for {getattr(so, 'name', '?')}",
		)

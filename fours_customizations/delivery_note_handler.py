"""
Delivery Note Handler — Fours Customizations
=============================================
on_submit
  When a Delivery Note **Return** is submitted, the matching Sales Invoice
  Return is kept in lock-step:

    • If the DN return was auto-created from a Sales Invoice Return, that
      invoice return is already submitted — nothing to do.
    • If a draft Sales Invoice Return exists against the original invoice,
      it is submitted automatically.
    • Otherwise a Sales Invoice Return (credit note) is created from the
      original invoice, sized to the returned quantities, and submitted.

on_trash
  When a Delivery Note is deleted, its sales chain is unwound, in order:

  1. Cancel every linked Sales Invoice (found via against_sales_invoice on the
     DN items). Done FIRST because the invoice holds the sales_order link on its
     items that would otherwise block the Sales Order cancellation. ignore_links
     is used so the auto-SO back-pointer (custom_source_sales_invoice) doesn't
     block it.
  2. For every Sales Order referenced by the deleted DN items:
       a. Skip if the SO is already cancelled or if other *submitted* DNs
          still exist for it (meaning the SO was partially fulfilled and
          the remaining notes are still live).
       b. Cancel all submitted Payment Entries whose reference_no = SO name.
       c. Cancel all submitted Stock Reservation Entries for the SO.
       d. Cancel the Sales Order itself — leaving it in a state where it
          can be amended and re-submitted after corrections.

  Every step skips documents that aren't currently submitted, so the handler is
  re-runnable and co-exists with the auto-cancel flow (which cancels the invoice
  before deleting the draft DN).
"""

import frappe
from frappe.utils import flt


# ── entry point ───────────────────────────────────────────────────────────────

def before_submit(doc, method=None):
	"""Silently enable negative stock on OOS items so DN submits cleanly (Req #5)."""
	if not frappe.db.get_value("Company", doc.company, "enable_selling_automations"):
		return
	try:
		from fours_customizations.negative_stock_handler import ensure_negative_stock_for_doc

		ensure_negative_stock_for_doc(doc)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S DN before_submit: negative stock check failed")


def on_submit(doc, method=None):
	"""Keep Sales Invoice Returns in lock-step with Delivery Note Returns."""
	if not frappe.db.get_value("Company", doc.company, "enable_selling_automations"):
		return
	if not doc.is_return:
		return
	try:
		_sync_sales_invoice_return(doc)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S DN return: SI return sync failed")
		frappe.msgprint(
			"Could not auto-submit the Sales Invoice Return for this Delivery Note "
			"Return — please create/submit it manually. Details are in the Error Log.",
			indicator="orange",
			alert=True,
		)


def on_trash(doc, method=None):
	"""Fired when a Delivery Note document is permanently deleted."""
	company = doc.company

	if not frappe.db.get_value("Company", company, "enable_selling_automations"):
		return

	# ── 1. Cancel linked Sales Invoices first ─────────────────────────────────
	# A submitted invoice holds the sales_order link on its items, which would
	# block the Sales Order cancellation below — so the invoice goes first.
	for si_name in _get_linked_sales_invoices(doc):
		_cancel_sales_invoice(si_name)

	# ── 2. Cancel Sales Orders (and their dependants) ─────────────────────────
	for so_name in _get_linked_sales_orders(doc):
		_cancel_sales_order_chain(so_name, deleted_dn=doc.name)


# ── DN return → SI return sync ───────────────────────────────────────────────

def _sync_sales_invoice_return(dn):
	"""Ensure a submitted Sales Invoice Return exists for this submitted
	Delivery Note Return.

	Per original Sales Invoice touched by the returned items:
	  1. Skip if this DN return was itself auto-created from a Sales Invoice
	     Return (that credit note is already submitted).
	  2. Submit an existing draft Sales Invoice Return, if one exists.
	  3. Otherwise create one from the original invoice, sized to the returned
	     quantities, interlink it to this DN return, and submit it.
	"""
	# Case 1 — DN return spawned by the SI-return flow (see sales_invoice_handler).
	if "Auto-created from Sales Invoice Return" in (dn.get("custom_remarks") or ""):
		return

	# Map returned rows back to the original Sales Invoice / SI item. The links
	# usually come across on the return rows; fall back to the original DN item.
	qty_by_si_item = {}     # {si_name: {si_detail: qty (negative)}}
	dn_row_by_si_item = {}  # {si_name: {si_detail: DN return item name}}
	for item in dn.items:
		si_name = item.get("against_sales_invoice")
		si_detail = item.get("si_detail")
		if (not si_name or not si_detail) and item.get("dn_detail"):
			orig = frappe.db.get_value(
				"Delivery Note Item",
				item.dn_detail,
				["against_sales_invoice", "si_detail"],
				as_dict=True,
			)
			if orig:
				si_name = si_name or orig.against_sales_invoice
				si_detail = si_detail or orig.si_detail
		if not si_name or not si_detail:
			continue
		qty_by_si_item.setdefault(si_name, {})
		qty_by_si_item[si_name][si_detail] = (
			qty_by_si_item[si_name].get(si_detail, 0) + flt(item.qty)
		)
		dn_row_by_si_item.setdefault(si_name, {})[si_detail] = item.name

	for si_name, si_item_qty in qty_by_si_item.items():
		if frappe.db.get_value("Sales Invoice", si_name, "docstatus") != 1:
			continue

		# Case 2 — a draft SI return already exists: submit it.
		draft_return = frappe.db.get_value(
			"Sales Invoice",
			{"is_return": 1, "return_against": si_name, "docstatus": 0},
			"name",
		)
		if draft_return:
			si_return = frappe.get_doc("Sales Invoice", draft_return)
			si_return.flags.from_dn_return = True
			si_return.flags.ignore_permissions = True
			si_return.submit()
			frappe.msgprint(
				f"Sales Invoice Return {si_return.name} submitted.", alert=True
			)
			continue

		# Case 3 — create + submit a new SI return sized to this DN return.
		_create_and_submit_si_return(
			dn, si_name, si_item_qty, dn_row_by_si_item.get(si_name, {})
		)


def _create_and_submit_si_return(dn, si_name, si_item_qty, dn_rows):
	"""Create a Sales Invoice Return (credit note) from `si_name` covering only
	the items/quantities on this Delivery Note Return, then submit it."""
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	si_return = make_return_doc("Sales Invoice", si_name)
	si_return.update_stock = 0
	si_return.is_pos = 0
	si_return.set_posting_time = 1
	si_return.posting_date = dn.posting_date
	si_return.posting_time = dn.posting_time

	kept = []
	for item in si_return.items:
		source_row = item.get("sales_invoice_item")
		if source_row and source_row in si_item_qty:
			item.qty = flt(si_item_qty[source_row])  # negative
			item.delivery_note = dn.name
			item.dn_detail = dn_rows.get(source_row)
			kept.append(item)
	if not kept:
		return
	si_return.set("items", kept)
	for idx, item in enumerate(si_return.items, start=1):
		item.idx = idx

	si_return.flags.from_dn_return = True
	si_return.flags.ignore_permissions = True
	si_return.insert(ignore_permissions=True)
	si_return.submit()
	frappe.msgprint(
		f"Sales Invoice Return {si_return.name} created and submitted for "
		f"Delivery Note Return {dn.name}.",
		alert=True,
	)


# ── helpers — linked document lookup ─────────────────────────────────────────


def _get_linked_sales_orders(doc) -> set:
	"""Return unique Sales Order names referenced by DN items."""
	return {
		item.against_sales_order
		for item in doc.items
		if getattr(item, "against_sales_order", None)
	}


def _get_linked_sales_invoices(doc) -> set:
	"""Return unique Sales Invoice names referenced by DN items."""
	return {
		item.against_sales_invoice
		for item in doc.items
		if getattr(item, "against_sales_invoice", None)
	}


def _cancel_sales_invoice(si_name: str):
	"""Cancel a linked Sales Invoice.

	Skips anything not currently submitted, so the handler is safe to re-run and
	to co-exist with the auto-cancel flow (which cancels the invoice before
	deleting the draft DN). ignore_links bypasses the auto-Sales-Order
	back-pointer (custom_source_sales_invoice) and payment links that would
	otherwise block the cancel — the order is torn down immediately afterwards.

	Guard: if *another* Delivery Note for this invoice is still submitted, the
	goods are out and the invoice must stand — cancelling it here would leave
	the sale reversed in the ledger while the stock stayed deducted. (The Sales
	Order teardown below has always had the equivalent guard; the invoice path
	did not, which is how SI-2627-039-3 and six others came apart from their
	notes.) Because ignore_links is set on this cancel, ERPNext's own check
	cannot catch it — so it is enforced here and, for every other cancellation
	route, in sales_chain_integrity.validate_no_submitted_delivery_note.
	"""
	if frappe.db.get_value("Sales Invoice", si_name, "docstatus") != 1:
		return

	from fours_customizations.sales_chain_integrity import (
		submitted_delivery_notes_for_invoice,
	)

	still_out = submitted_delivery_notes_for_invoice(si_name)
	if still_out:
		frappe.msgprint(
			f"Sales Invoice {si_name} was left submitted: Delivery Note "
			f"{', '.join(still_out)} is still submitted, so its stock is out. "
			f"Return or cancel that Delivery Note first if the invoice must go.",
			indicator="orange",
			alert=True,
		)
		return

	si = frappe.get_doc("Sales Invoice", si_name)
	si.flags.ignore_permissions = True
	si.flags.ignore_links = True
	si.cancel()
	frappe.msgprint(f"Sales Invoice {si_name} cancelled.", alert=True)


# ── cancellation chain ────────────────────────────────────────────────────────

def _cancel_sales_order_chain(so_name: str, deleted_dn: str):
	"""
	Cancel everything tied to a Sales Order, then cancel the SO itself.

	Dependency order (innermost first so nothing blocks the SO cancel):
	  Payment Entries  →  Stock Reservation Entries  →  Sales Order
	"""
	so = frappe.get_doc("Sales Order", so_name)

	# Already cancelled — nothing to do
	if so.docstatus == 2:
		return

	# Only submitted SOs need processing
	if so.docstatus != 1:
		return

	# Guard: if other submitted Delivery Notes still reference this SO,
	# the order is only partially fulfilled — do not cancel it.
	other_submitted_dns = frappe.get_all(
		"Delivery Note Item",
		filters={
			"against_sales_order": so_name,
			"docstatus": 1,
			"parent": ["!=", deleted_dn],
		},
		pluck="parent",
		distinct=True,
	)
	if other_submitted_dns:
		frappe.msgprint(
			f"Sales Order {so_name} has other submitted Delivery Notes "
			f"({', '.join(other_submitted_dns)}) — skipping cancellation.",
			alert=True,
		)
		return

	# ── a. Payment Entries ────────────────────────────────────────────────────
	_cancel_payment_entries(so_name, so.customer, so.company)

	# ── b. Stock Reservation Entries ──────────────────────────────────────────
	_cancel_stock_reservations(so_name)

	# ── c. Sales Order ────────────────────────────────────────────────────────
	# Allow cancel even if commission JEs or other links exist
	so.flags.ignore_links = True
	so.cancel()
	frappe.msgprint(
		f"Sales Order {so_name} cancelled — it can now be amended.",
		alert=True,
	)


def _cancel_payment_entries(so_name: str, customer: str, company: str):
	"""Cancel every submitted Payment Entry whose reference_no equals the SO."""
	pe_names = frappe.get_all(
		"Payment Entry",
		filters={
			"reference_no": so_name,
			"party_type": "Customer",
			"party": customer,
			"payment_type": "Receive",
			"company": company,
			"docstatus": 1,
		},
		pluck="name",
		order_by="creation desc",
	)

	for pe_name in pe_names:
		pe = frappe.get_doc("Payment Entry", pe_name)
		# Allow cancel even if commission JEs reference this PE
		pe.flags.ignore_links = True
		pe.cancel()
		frappe.msgprint(f"Cancelled Payment Entry {pe_name}.", alert=True)


def _cancel_stock_reservations(so_name: str):
	"""
	Cancel every submitted Stock Reservation Entry for this Sales Order.

	ERPNext stores these with:
	  voucher_type = 'Sales Order'
	  voucher_no   = <so_name>
	  docstatus    = 1
	"""
	sre_names = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"voucher_type": "Sales Order",
			"voucher_no": so_name,
			"docstatus": 1,
		},
		pluck="name",
		order_by="creation desc",
	)

	for sre_name in sre_names:
		sre = frappe.get_doc("Stock Reservation Entry", sre_name)
		sre.flags.ignore_links = True
		sre.cancel()
		frappe.msgprint(f"Cancelled Stock Reservation Entry {sre_name}.", alert=True)
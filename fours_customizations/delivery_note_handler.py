"""
Delivery Note Handler — Fours Customizations
=============================================
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
	"""
	if frappe.db.get_value("Sales Invoice", si_name, "docstatus") != 1:
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
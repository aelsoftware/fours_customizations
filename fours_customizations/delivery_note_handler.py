"""
Delivery Note Handler — Fours Customizations
=============================================
on_trash
  When a Delivery Note is deleted the following chain is executed in order:

  1. Cancel linked Sales Invoices (existing behaviour, unchanged).
  2. For every Sales Order referenced by the deleted DN items:
       a. Skip if the SO is already cancelled or if other *submitted* DNs
          still exist for it (meaning the SO was partially fulfilled and
          the remaining notes are still live).
       b. Cancel all submitted Payment Entries whose reference_no = SO name.
       c. Cancel all submitted Stock Reservation Entries for the SO.
       d. Cancel the Sales Order itself — leaving it in a state where it
          can be amended and re-submitted after corrections.
"""

import frappe


# ── entry point ───────────────────────────────────────────────────────────────

def on_trash(doc, method=None):
	"""Fired when a Delivery Note document is permanently deleted."""
	company = doc.company

	if not frappe.db.get_value("Company", company, "enable_selling_automations"):
		return

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
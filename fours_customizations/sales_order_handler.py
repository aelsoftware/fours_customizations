"""
Sales Order Handler — Fours Customizations
==========================================
When a Sales Order with custom_include_payment=1 is submitted, this module:
  1. Validates that: sum(custom_payments) − custom_change_amount == grand_total
  2. Distributes the change against the payment row whose account matches
     custom_account_for_change_amount (fallback: last row).
  3. Creates one submitted Payment Entry per effective payment row.

On cancellation every PE created for this SO is cancelled in reverse order.
"""

import frappe
from frappe.utils import flt, nowdate
from erpnext.accounts.party import get_party_account

# ── helpers ──────────────────────────────────────────────────────────────────

def _automation_enabled(company: str) -> bool:
	return bool(frappe.db.get_value("Company", company, "enable_selling_automations"))


# ── hooks ─────────────────────────────────────────────────────────────────────

def on_submit(doc, method=None):
	"""Entry point: validate payments and create Payment Entries."""
	if not flt(doc.custom_include_payment):
		return
	if not _automation_enabled(doc.company):
		return

	_validate_payments(doc)
	_create_payment_entries(doc)


def on_cancel(doc, method=None):
	"""Cancel all Payment Entries that were created for this Sales Order."""
	if not _automation_enabled(doc.company):
		return

	pes = frappe.get_all(
		"Payment Entry",
		filters={
			"reference_no": doc.name,
			"party_type": "Customer",
			"party": doc.customer,
			"payment_type": "Receive",
			"docstatus": 1,
		},
		pluck="name",
		order_by="creation desc",
	)

	for pe_name in pes:
		pe = frappe.get_doc("Payment Entry", pe_name)
		pe.cancel()
		frappe.msgprint(f"Payment Entry {pe_name} cancelled.", alert=True)


# ── validation ────────────────────────────────────────────────────────────────

def _validate_payments(doc):
	"""
	Strict validation rules:
	  • custom_payments must be non-empty
	  • every row must have mode_of_payment and account
	  • change_amount >= 0
	  • if change_amount > 0, custom_account_for_change_amount must be set
	  • sum(custom_payments.amount) − change_amount == grand_total  (2-decimal tolerance)
	  • change_amount < sum(custom_payments.amount)   (can't give more change than collected)
	"""
	if not doc.custom_payments:
		frappe.throw(
			"No payment rows found in <b>Payments</b> table. "
			"Add at least one payment method before submitting."
		)

	grand_total = flt(doc.grand_total, 2)
	change = flt(doc.custom_change_amount, 2)
	total_collected = flt(sum(flt(p.amount) for p in doc.custom_payments), 2)

	# ── row-level checks ──
	for i, p in enumerate(doc.custom_payments, start=1):
		if not p.mode_of_payment:
			frappe.throw(f"Payment row {i}: <b>Mode of Payment</b> is required.")
		if not p.account:
			frappe.throw(
				f"Payment row {i} ({p.mode_of_payment}): <b>Account</b> is missing. "
				"Check the Mode of Payment configuration."
			)
		if flt(p.amount) <= 0:
			frappe.throw(
				f"Payment row {i} ({p.mode_of_payment}): Amount must be greater than zero."
			)

	# ── change checks ──
	if change < 0:
		frappe.throw("Change amount cannot be negative.")

	if change >= total_collected:
		frappe.throw(
			f"Change amount ({change:,.2f}) must be less than the total collected "
			f"({total_collected:,.2f})."
		)

	if change > 0 and not doc.custom_account_for_change_amount:
		frappe.throw(
			"Change amount is set but <b>Account for Change Amount</b> is not configured."
		)

	# ── net payment check ──
	net_paid = flt(total_collected - change, 2)
	if net_paid != grand_total:
		frappe.throw(
			f"Payment mismatch:<br>"
			f"&nbsp;&nbsp;Collected &nbsp;&nbsp;&nbsp;: <b>{total_collected:,.2f}</b><br>"
			f"&nbsp;&nbsp;Change &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: <b>{change:,.2f}</b><br>"
			f"&nbsp;&nbsp;Net received : <b>{net_paid:,.2f}</b><br>"
			f"&nbsp;&nbsp;Order total &nbsp;: <b>{grand_total:,.2f}</b><br><br>"
			"Net received must equal the order total. "
			"Adjust the payment amounts or the change amount."
		)


# ── payment creation ──────────────────────────────────────────────────────────

def _effective_payments(doc) -> list[dict]:
	"""
	Return a list of {mode, account, amount} after deducting the change.

	Strategy:
	  1. Find the first row whose account matches custom_account_for_change_amount
	     and subtract as much change as that row can absorb.
	  2. If change is still unabsorbed (e.g. no matching account), keep deducting
	     from the last row(s) in reverse order.
	  3. Drop rows whose effective amount is <= 0.
	"""
	change_account = doc.custom_account_for_change_amount or ""
	remaining = flt(doc.custom_change_amount, 2)

	rows = [
		{"mode": p.mode_of_payment, "account": p.account, "amount": flt(p.amount, 2)}
		for p in doc.custom_payments
	]

	# Pass 1: absorb from matching account row(s)
	if remaining > 0 and change_account:
		for row in rows:
			if row["account"] == change_account and remaining > 0:
				absorb = min(row["amount"], remaining)
				row["amount"] = flt(row["amount"] - absorb, 2)
				remaining = flt(remaining - absorb, 2)

	# Pass 2: absorb any remainder from the last non-zero rows (reverse)
	if remaining > 0:
		for row in reversed(rows):
			if row["amount"] > 0 and remaining > 0:
				absorb = min(row["amount"], remaining)
				row["amount"] = flt(row["amount"] - absorb, 2)
				remaining = flt(remaining - absorb, 2)

	# Safety: if somehow remaining > 0, raise (should never happen after _validate_payments)
	if remaining > 0:
		frappe.throw(
			f"Could not fully distribute the change amount. "
			f"Unabsorbed change: {remaining:,.2f}. Please review payment rows."
		)

	return [r for r in rows if r["amount"] > 0]


def _create_payment_entries(doc):
	effective = _effective_payments(doc)
	if not effective:
		frappe.msgprint("No effective payment amounts to record.", alert=True)
		return

	receivable_account = get_party_account("Customer", doc.customer, doc.company)
	cost_center = frappe.get_cached_value("Company", doc.company, "cost_center")
	posting_date = doc.transaction_date or nowdate()
	created = []

	for row in effective:
		pe = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"posting_date": posting_date,
			"company": doc.company,
			"party_type": "Customer",
			"party": doc.customer,
			"party_name": doc.customer_name,
			"mode_of_payment": row["mode"],
			# Receivable account → gets credited (reduces what customer owes)
			"paid_from": receivable_account,
			"paid_from_account_currency": doc.currency,
			# Cash / bank account → gets debited (money lands here)
			"paid_to": row["account"],
			"paid_to_account_currency": doc.currency,
			"paid_amount": row["amount"],
			"received_amount": row["amount"],
			"source_exchange_rate": flt(doc.conversion_rate) or 1,
			"target_exchange_rate": flt(doc.conversion_rate) or 1,
			"reference_no": doc.name,
			"reference_date": posting_date,
			"cost_center": cost_center,
			"currency": doc.currency,
			"remarks": (
				f"Payment received against Sales Order {doc.name} "
				f"via {row['mode']}."
			),
		})

		pe.insert(ignore_permissions=True)
		pe.submit()
		created.append(pe.name)

	if created:
		label = "Payment Entries" if len(created) > 1 else "Payment Entry"
		frappe.msgprint(
			f"{label} {', '.join(created)} created for {doc.name}.",
			alert=True,
		)
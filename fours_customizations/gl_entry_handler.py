import frappe
from frappe.utils import flt
from erpnext.accounts.party import get_party_account


def _is_automation_enabled(company):
	"""Check if selling automations are enabled for this company."""
	return frappe.db.get_value("Company", company, "enable_selling_automations")


def on_submit(doc, method=None):
	"""Create or cancel commission JEs based on GL Entry affecting receivable accounts."""
	if doc.flags.get("from_repost"):
		return

	account_type = frappe.get_cached_value("Account", doc.account, "account_type")
	if account_type != "Receivable":
		return

	if not _is_automation_enabled(doc.company):
		return

	if doc.against_voucher_type != "Sales Invoice" or not doc.against_voucher:
		return

	# Credit on receivable + not cancelled = payment received → create commission
	if flt(doc.credit) > 0 and not doc.is_cancelled:
		_create_commission_for_gl(doc)
	# Cancellation reverse entry → create reversal JEs
	elif flt(doc.debit) > 0 and doc.is_cancelled:
		_reverse_commission_for_gl(doc)


def _create_commission_for_gl(doc):
	"""Create a commission JE when a receivable account is credited."""
	si = frappe.get_doc("Sales Invoice", doc.against_voucher)

	# Credit notes are handled in sales_invoice_handler.on_submit
	if si.is_return:
		return

	if not si.sales_partner or flt(si.total_commission) <= 0:
		return

	if flt(si.base_grand_total) <= 0:
		return

	# Duplicate prevention: skip if commission JE already exists for this voucher + SI
	if frappe.db.exists("Journal Entry", {
		"custom_commission_voucher_no": doc.voucher_no,
		"custom_commission_sales_invoice": si.name,
		"docstatus": ["!=", 2],
	}):
		return

	paid_ratio = min(flt(doc.credit) / flt(si.base_grand_total), 1.0)
	commission = flt(paid_ratio * flt(si.total_commission), 2)

	if commission <= 0:
		return

	supplier = frappe.db.get_value("Sales Partner", si.sales_partner, "custom_supplier_account")
	if not supplier:
		frappe.msgprint(
			f"Sales Partner {si.sales_partner} has no linked Supplier (custom_supplier_account). "
			"Skipping commission Journal Entry.",
			alert=True,
		)
		return

	expense_account = frappe.db.get_value(
		"Company", doc.company, "sales_commission_expense_account"
	)
	if not expense_account:
		frappe.throw(
			f"Please configure the Sales Commission Expense Account on the Selling Automations tab "
			f"in Company {doc.company} before submitting."
		)

	creditors_account = get_party_account("Supplier", supplier, doc.company)
	cost_center = si.cost_center or frappe.get_cached_value("Company", doc.company, "cost_center")

	je = frappe.get_doc({
		"doctype": "Journal Entry",
		"voucher_type": "Journal Entry",
		"posting_date": doc.posting_date,
		"company": doc.company,
		"user_remark": f"Commission for {doc.voucher_no} allocation to {si.name}",
		"custom_commission_payment_entry": doc.voucher_no if doc.voucher_type == "Payment Entry" else None,
		"custom_commission_sales_invoice": si.name,
		"custom_commission_voucher_no": doc.voucher_no,
		"accounts": [
			{
				"account": expense_account,
				"debit_in_account_currency": commission,
				"cost_center": cost_center,
			},
			{
				"account": creditors_account,
				"credit_in_account_currency": commission,
				"party_type": "Supplier",
				"party": supplier,
			},
		],
	})
	je.insert(ignore_permissions=True)
	je.submit()

	frappe.msgprint(f"Commission Journal Entry {je.name} created for {si.name}.", alert=True)


def _reverse_commission_for_gl(doc):
	"""Create reversal JEs when a receivable GL entry is reversed (payment cancelled)."""
	commission_jes = frappe.get_all("Journal Entry", filters={
		"custom_commission_voucher_no": doc.voucher_no,
		"custom_commission_sales_invoice": doc.against_voucher,
		"docstatus": 1,
	}, pluck="name")

	for je_name in commission_jes:
		_create_reversal_je(je_name, reason=f"payment {doc.voucher_no} cancelled")


def _create_reversal_je(original_je_name, reason=""):
	"""Create an opposite JE to reverse a commission JE. Keeps the original intact."""
	# Duplicate prevention: skip if reversal already exists
	if frappe.db.exists("Journal Entry", {
		"custom_commission_voucher_no": f"REV-{original_je_name}",
		"docstatus": ["!=", 2],
	}):
		return

	original = frappe.get_doc("Journal Entry", original_je_name)

	# Swap debit ↔ credit on each account row
	reversed_accounts = []
	for row in original.accounts:
		reversed_accounts.append({
			"account": row.account,
			"debit_in_account_currency": flt(row.credit_in_account_currency),
			"credit_in_account_currency": flt(row.debit_in_account_currency),
			"party_type": row.party_type,
			"party": row.party,
			"cost_center": row.cost_center,
		})

	je = frappe.get_doc({
		"doctype": "Journal Entry",
		"voucher_type": "Journal Entry",
		"posting_date": frappe.utils.today(),
		"company": original.company,
		"user_remark": f"Reversal of {original_je_name} ({reason})",
		"custom_commission_payment_entry": original.custom_commission_payment_entry,
		"custom_commission_sales_invoice": original.custom_commission_sales_invoice,
		"custom_commission_voucher_no": f"REV-{original_je_name}",
		"accounts": reversed_accounts,
	})
	je.insert(ignore_permissions=True, ignore_links=True)
	je.submit()

	frappe.msgprint(
		f"Reversal Journal Entry {je.name} created for {original_je_name} ({reason}).",
		alert=True,
	)


def on_unreconcile(doc, method=None):
	"""Create reversal JEs when a payment is unreconciled from invoices.

	Unreconciliation updates GL entries via SQL (clears against_voucher),
	so the GL Entry on_submit hook never fires. We handle it here.
	"""
	if not _is_automation_enabled(doc.company):
		return

	voucher_no = doc.voucher_no
	for alloc in doc.allocations:
		if alloc.reference_doctype != "Sales Invoice":
			continue

		si_name = alloc.reference_name
		commission_jes = frappe.get_all("Journal Entry", filters={
			"custom_commission_voucher_no": voucher_no,
			"custom_commission_sales_invoice": si_name,
			"docstatus": 1,
		}, pluck="name")

		for je_name in commission_jes:
			_create_reversal_je(je_name, reason=f"{voucher_no} unreconciled from {si_name}")

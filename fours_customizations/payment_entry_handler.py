import frappe
from frappe.utils import flt
from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_outstanding_invoices
from erpnext.accounts.doctype.payment_entry.payment_entry import get_reference_details


def _is_automation_enabled(company):
	"""Check if selling automations are enabled for this company."""
	return frappe.db.get_value("Company", company, "enable_selling_automations")


def before_cancel(doc, method=None):
	"""Allow PE cancellation even when commission JEs link back to it."""
	if not _is_automation_enabled(doc.company):
		return

	if frappe.db.exists("Journal Entry", {
		"custom_commission_payment_entry": doc.name,
		"docstatus": 1,
	}):
		doc.flags.ignore_links = True


def before_submit(doc, method=None):
	"""Auto-allocate unallocated payment amount against outstanding Sales Invoices (FIFO)."""
	if not _is_automation_enabled(doc.company):
		return

	if doc.payment_type != "Receive" or doc.party_type != "Customer":
		return

	remaining = flt(doc.unallocated_amount)
	if remaining <= 0:
		return

	receivable_account = get_party_account("Customer", doc.party, doc.company)
	outstanding = get_outstanding_invoices(
		"Customer",
		doc.party,
		[receivable_account],
	)

	# Sort by posting_date then name for strict FIFO
	outstanding.sort(key=lambda inv: (inv.posting_date, inv.voucher_no))

	for inv in outstanding:
		if inv.voucher_type != "Sales Invoice":
			continue

		alloc = min(remaining, flt(inv.outstanding_amount))
		if alloc <= 0:
			continue

		ref_details = get_reference_details(
			"Sales Invoice",
			inv.voucher_no,
			doc.party_account_currency,
			doc.party_type,
			doc.party,
		)

		doc.append("references", {
			"reference_doctype": "Sales Invoice",
			"reference_name": inv.voucher_no,
			"due_date": ref_details.due_date,
			"total_amount": ref_details.total_amount,
			"outstanding_amount": ref_details.outstanding_amount,
			"allocated_amount": alloc,
			"exchange_rate": ref_details.exchange_rate,
			"bill_no": ref_details.bill_no,
			"account": ref_details.get("account"),
		})

		remaining -= alloc
		if remaining <= 0:
			break

	# Recalculate totals so GL entries and difference_amount are correct
	doc.set_amounts()

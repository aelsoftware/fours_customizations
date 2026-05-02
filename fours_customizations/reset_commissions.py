"""
Reset and regenerate all commission Journal Entries.

Usage:
    bench --site <site> execute fours_customizations.reset_commissions.run

This will:
1. Delete all commission JEs and their GL/ledger entries via direct SQL
2. Regenerate from GL Entries (payments) and return SIs (credit notes)

Safe to run on live — deletion is pure SQL (no hooks), regeneration
monkey-patches frappe.enqueue to prevent background job overflow.
"""
import frappe
from frappe.utils import flt
from erpnext.accounts.party import get_party_account


def run():
	frappe.flags.mute_messages = True

	company = "4S Industries Limited"
	if not frappe.db.get_value("Company", company, "enable_selling_automations"):
		print(f"ERROR: Selling automations not enabled for {company}")
		return

	print("=" * 60)
	print("COMMISSION JE RESET & REGENERATION")
	print(f"Company: {company}")
	print("=" * 60)

	# Monkey-patch frappe.enqueue to no-op so other apps' hooks
	# (e.g. saleslive Firebase) can't overflow the background job queue.
	_original_enqueue = frappe.enqueue
	frappe.enqueue = lambda *args, **kwargs: None
	try:
		_do_reset(company)
	finally:
		frappe.enqueue = _original_enqueue


def _do_reset(company):
	# ── Phase 1: Delete ALL commission JEs via direct SQL ──
	print("\n--- Phase 1: Delete all commission JEs (direct SQL) ---")

	all_jes = frappe.db.sql("""
		SELECT name, docstatus
		FROM `tabJournal Entry`
		WHERE custom_commission_sales_invoice IS NOT NULL
			AND custom_commission_sales_invoice != ''
			AND company = %s
	""", (company,), as_dict=True)

	je_names = [je.name for je in all_jes]
	submitted_count = sum(1 for je in all_jes if je.docstatus == 1)
	cancelled_count = sum(1 for je in all_jes if je.docstatus == 2)

	print(f"  Found {len(je_names)} commission JEs ({submitted_count} submitted, {cancelled_count} cancelled)")

	if je_names:
		# Delete GL entries
		frappe.db.sql("""
			DELETE FROM `tabGL Entry`
			WHERE voucher_type = 'Journal Entry'
				AND voucher_no IN %s
		""", [je_names])

		# Delete Payment Ledger entries
		frappe.db.sql("""
			DELETE FROM `tabPayment Ledger Entry`
			WHERE voucher_type = 'Journal Entry'
				AND voucher_no IN %s
		""", [je_names])

		# Delete JE Account child rows
		frappe.db.sql("""
			DELETE FROM `tabJournal Entry Account`
			WHERE parent IN %s
		""", [je_names])

		# Delete the JEs themselves
		frappe.db.sql("""
			DELETE FROM `tabJournal Entry`
			WHERE name IN %s
		""", [je_names])

		frappe.db.commit()

	print(f"  Deleted {len(je_names)} commission JEs and their ledger entries")

	# ── Phase 2: Regenerate from GL Entries ──
	print("\n--- Phase 2: Regenerate from GL Entries ---")

	receivable_accounts = frappe.get_all("Account", filters={
		"company": company,
		"account_type": "Receivable",
	}, pluck="name")

	if not receivable_accounts:
		print("  ERROR: No receivable accounts found")
		return

	gl_entries = frappe.db.sql("""
		SELECT
			gle.name,
			gle.posting_date,
			gle.account,
			gle.credit,
			gle.voucher_type,
			gle.voucher_no,
			gle.against_voucher_type,
			gle.against_voucher,
			gle.company
		FROM `tabGL Entry` gle
		WHERE gle.company = %s
			AND gle.account IN %s
			AND gle.credit > 0
			AND gle.is_cancelled = 0
			AND gle.against_voucher_type = 'Sales Invoice'
			AND gle.against_voucher IS NOT NULL
			AND gle.against_voucher != ''
		ORDER BY gle.posting_date, gle.name
	""", (company, receivable_accounts), as_dict=True)

	print(f"  Found {len(gl_entries)} qualifying GL Entries")

	si_cache = {}
	created = 0
	skipped = 0

	expense_account = frappe.db.get_value("Company", company, "sales_commission_expense_account")
	if not expense_account:
		print(f"  ERROR: No sales_commission_expense_account configured for {company}")
		return

	for i, gle in enumerate(gl_entries, 1):
		si_name = gle.against_voucher

		if si_name not in si_cache:
			si_cache[si_name] = frappe.db.get_value("Sales Invoice", si_name, [
				"name", "sales_partner", "total_commission", "base_grand_total",
				"is_return", "cost_center", "posting_date",
			], as_dict=True)

		si = si_cache[si_name]
		if not si:
			skipped += 1
			continue

		if si.is_return:
			skipped += 1
			continue

		if not si.sales_partner or flt(si.total_commission) <= 0:
			skipped += 1
			continue

		if flt(si.base_grand_total) <= 0:
			skipped += 1
			continue

		# Duplicate prevention (multiple GL entries for same voucher+SI)
		if frappe.db.exists("Journal Entry", {
			"custom_commission_voucher_no": gle.voucher_no,
			"custom_commission_sales_invoice": si_name,
			"docstatus": ["!=", 2],
		}):
			skipped += 1
			continue

		paid_ratio = min(flt(gle.credit) / flt(si.base_grand_total), 1.0)
		commission = flt(paid_ratio * flt(si.total_commission), 2)

		if commission <= 0:
			skipped += 1
			continue

		supplier = frappe.db.get_value("Sales Partner", si.sales_partner, "custom_supplier_account")
		if not supplier:
			skipped += 1
			continue

		creditors_account = get_party_account("Supplier", supplier, company)
		cost_center = si.cost_center or frappe.get_cached_value("Company", company, "cost_center")

		je = frappe.get_doc({
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"posting_date": gle.posting_date,
			"company": company,
			"user_remark": f"Commission for {gle.voucher_no} allocation to {si_name}",
			"custom_commission_payment_entry": gle.voucher_no if gle.voucher_type == "Payment Entry" else None,
			"custom_commission_sales_invoice": si_name,
			"custom_commission_voucher_no": gle.voucher_no,
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
		created += 1

		if created % 50 == 0:
			frappe.db.commit()
			print(f"  Created {created} JEs so far (processed {i}/{len(gl_entries)})...")

	frappe.db.commit()
	print(f"  Created {created} commission JEs ({skipped} GL entries skipped)")

	# ── Phase 3: Regenerate credit note commissions ──
	print("\n--- Phase 3: Regenerate credit note commissions ---")

	return_invoices = frappe.db.sql("""
		SELECT
			name, posting_date, sales_partner, total_commission,
			base_grand_total, return_against, cost_center, company
		FROM `tabSales Invoice`
		WHERE company = %s
			AND docstatus = 1
			AND is_return = 1
			AND sales_partner IS NOT NULL
			AND sales_partner != ''
			AND total_commission < 0
		ORDER BY posting_date, name
	""", (company,), as_dict=True)

	print(f"  Found {len(return_invoices)} credit notes with commission")
	cn_created = 0

	for cn in return_invoices:
		commission = abs(flt(cn.total_commission, 2))
		if commission <= 0:
			continue

		# Duplicate prevention
		if frappe.db.exists("Journal Entry", {
			"custom_commission_voucher_no": cn.name,
			"custom_commission_sales_invoice": cn.name,
			"docstatus": ["!=", 2],
		}):
			continue

		supplier = frappe.db.get_value("Sales Partner", cn.sales_partner, "custom_supplier_account")
		if not supplier:
			continue

		creditors_account = get_party_account("Supplier", supplier, company)
		cost_center = cn.cost_center or frappe.get_cached_value("Company", company, "cost_center")

		je = frappe.get_doc({
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"posting_date": cn.posting_date,
			"company": company,
			"user_remark": f"Commission reduction for credit note {cn.name} against {cn.return_against or ''}",
			"custom_commission_sales_invoice": cn.name,
			"custom_commission_voucher_no": cn.name,
			"accounts": [
				{
					"account": creditors_account,
					"debit_in_account_currency": commission,
					"party_type": "Supplier",
					"party": supplier,
				},
				{
					"account": expense_account,
					"credit_in_account_currency": commission,
					"cost_center": cost_center,
				},
			],
		})
		je.insert(ignore_permissions=True)
		je.submit()
		cn_created += 1

		if cn_created % 50 == 0:
			frappe.db.commit()
			print(f"  Created {cn_created} credit note JEs so far...")

	frappe.db.commit()
	print(f"  Created {cn_created} credit note commission JEs")

	# ── Summary ──
	print("\n" + "=" * 60)
	print("COMPLETE")
	print(f"  Deleted:       {len(je_names)} old commission JEs")
	print(f"  Regenerated:   {created} payment commission JEs")
	print(f"  Credit notes:  {cn_created} credit note commission JEs")
	print(f"  Total new JEs: {created + cn_created}")
	print("=" * 60)

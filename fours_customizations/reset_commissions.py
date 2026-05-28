"""
reset_commissions.py — One-shot cleanup of legacy commission Journal Entries.

The commission flow used to book a Journal Entry per payment received
(Dr Commission Expense / Cr Sales Partner Supplier).  That path has been
retired — commission is now computed on the fly during Salary Slip
calculation and paid out via payroll.

Running this script deletes every JE that the old flow created, along with
its GL entries, payment ledger entries, and child rows.  It does not
re-create anything.

Usage:
    bench --site <site> execute fours_customizations.reset_commissions.run \\
        --kwargs "{'company': '4S Industries Limited'}"

Safe to run multiple times — only matches JEs tagged with the legacy
custom_commission_sales_invoice field.
"""

import frappe


def run(company: str | None = None) -> None:
	"""Delete all legacy commission Journal Entries (optionally for one company)."""
	filters = ["custom_commission_sales_invoice IS NOT NULL", "custom_commission_sales_invoice != ''"]
	params: list = []
	if company:
		filters.append("company = %s")
		params.append(company)

	rows = frappe.db.sql(
		f"SELECT name, docstatus FROM `tabJournal Entry` WHERE {' AND '.join(filters)}",
		tuple(params),
		as_dict=True,
	)
	names = [r.name for r in rows]
	submitted = sum(1 for r in rows if r.docstatus == 1)
	cancelled = sum(1 for r in rows if r.docstatus == 2)

	print(f"Found {len(names)} legacy commission JEs ({submitted} submitted, {cancelled} cancelled).")
	if not names:
		return

	frappe.db.sql("DELETE FROM `tabGL Entry` WHERE voucher_type = 'Journal Entry' AND voucher_no IN %s", [names])
	frappe.db.sql("DELETE FROM `tabPayment Ledger Entry` WHERE voucher_type = 'Journal Entry' AND voucher_no IN %s", [names])
	frappe.db.sql("DELETE FROM `tabJournal Entry Account` WHERE parent IN %s", [names])
	frappe.db.sql("DELETE FROM `tabJournal Entry` WHERE name IN %s", [names])
	frappe.db.commit()

	print(f"Deleted {len(names)} JEs and their ledger entries.")

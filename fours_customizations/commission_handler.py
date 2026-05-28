"""
commission_handler.py — Sales Person commission engine.

Model
-----
* `Sales Invoice.custom_sales_person` → who owns the sale (drives total_sales).
* `Payment Entry.sales_person`        → who collected the money (drives
  total_payments and commission).
* `Sales Person.commission_rate`      → percentage rate applied to payments.

Commission formula (simple, what the user signed off on):

    commission = total_payments * commission_rate / 100

Half-paid invoices split naturally: this month's slip sees only the payments
received in this month's window.

No Journal Entries are booked here.  Salary Slip pulls the commission as an
earning at payroll time.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate


# ── per-Sales-Person aggregates ───────────────────────────────────────────


def get_sales_person_summary(sales_person: str, start, end, company: str | None = None) -> dict:
	"""Return total_sales, total_payments, commission_rate, total_commission
	for one Sales Person across [start, end]."""
	if not sales_person:
		return {
			"sales_person": "",
			"total_sales": 0.0,
			"total_payments": 0.0,
			"commission_rate": 0.0,
			"total_commission": 0.0,
		}

	rate = flt(frappe.db.get_value("Sales Person", sales_person, "commission_rate"))
	start_d = getdate(start)
	end_d = getdate(end)

	# Total sales — Sales Invoices owned by this Sales Person, posted in window.
	si_conditions = [
		"si.docstatus = 1",
		"si.is_return = 0",
		"si.custom_sales_person = %(sp)s",
		"si.posting_date BETWEEN %(start)s AND %(end)s",
	]
	si_params: dict = {"sp": sales_person, "start": start_d, "end": end_d}
	if company:
		si_conditions.append("si.company = %(company)s")
		si_params["company"] = company

	total_sales = flt(
		frappe.db.sql(
			f"SELECT COALESCE(SUM(si.base_grand_total), 0) FROM `tabSales Invoice` si WHERE {' AND '.join(si_conditions)}",
			si_params,
		)[0][0]
	)

	# Total payments — incoming Payment Entries credited to this Sales Person.
	pe_conditions = [
		"pe.docstatus = 1",
		"pe.payment_type = 'Receive'",
		"pe.sales_person = %(sp)s",
		"pe.posting_date BETWEEN %(start)s AND %(end)s",
	]
	pe_params: dict = {"sp": sales_person, "start": start_d, "end": end_d}
	if company:
		pe_conditions.append("pe.company = %(company)s")
		pe_params["company"] = company

	total_payments = flt(
		frappe.db.sql(
			f"SELECT COALESCE(SUM(pe.paid_amount), 0) FROM `tabPayment Entry` pe WHERE {' AND '.join(pe_conditions)}",
			pe_params,
		)[0][0]
	)

	commission = flt(total_payments * rate / 100.0, 2)
	return {
		"sales_person": sales_person,
		"total_sales": flt(total_sales, 2),
		"total_payments": flt(total_payments, 2),
		"commission_rate": rate,
		"total_commission": commission,
	}


def compute_sales_person_commission(sales_person: str, start, end, company: str | None = None) -> float:
	return get_sales_person_summary(sales_person, start, end, company)["total_commission"]


def compute_employee_commission(employee: str, start, end, company: str | None = None) -> float:
	"""Total commission across all Sales Person records linked to `employee`."""
	if not employee:
		return 0.0
	persons = frappe.get_all(
		"Sales Person",
		filters={"employee": employee, "enabled": 1},
		pluck="name",
	)
	if not persons:
		return 0.0
	total = 0.0
	for sp in persons:
		total += compute_sales_person_commission(sp, start, end, company)
	return flt(total, 2)

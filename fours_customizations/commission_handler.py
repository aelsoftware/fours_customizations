"""
commission_handler.py — Sales Partner commission engine (Req #4).

Single source of truth: the Salary Slip.

For each Sales Partner linked to an employee, this module sums the
commission earned across all Sales Invoices in the payroll period, using the
formula:

    commission = (rate / 100) * amount_eligible_for_commission * (paid_in_period / base_grand_total)

Where:
  - `rate` is the Sales Partner's `commission_rate`
  - `amount_eligible_for_commission` is the standard ERPNext field on the
    Sales Invoice (computed from items where `grant_commission = 1`)
  - `paid_in_period` is the sum of credits on the SI's receivable account
    posted between the salary period's start and end dates
  - `base_grand_total` is the SI total

Worked example
--------------
    invoice_total                  = 1,000,000
    amount_eligible_for_commission =   800,000
    paid_in_period                 =   500,000
    rate                           =   0.4 %
    commission                     = 0.004 * 800,000 * (500,000/1,000,000)
                                   = 1,600

This module DOES NOT book any Journal Entries.  Commission flows entirely
through the Salary Slip as an earning — payroll is the only thing that
moves money.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate


def get_partner_commission_breakdown(partner: str, start, end, company: str | None = None) -> dict:
	"""Compute commission earned by `partner` for payments received in [start, end].

	Returns:
	    {
	        "total_commission": float,
	        "total_paid": float,
	        "rows": [
	            {
	                "sales_invoice": str,
	                "invoice_total": float,
	                "eligible_amount": float,
	                "paid_in_window": float,
	                "rate": float,
	                "commission": float,
	            },
	            ...
	        ]
	    }
	"""
	if not partner:
		return {"total_commission": 0.0, "total_paid": 0.0, "rows": []}

	rate = flt(frappe.db.get_value("Sales Partner", partner, "commission_rate"))
	if rate <= 0:
		return {"total_commission": 0.0, "total_paid": 0.0, "rows": []}

	filters = ["si.docstatus = 1", "si.sales_partner = %(partner)s", "si.is_return = 0"]
	params: dict = {"partner": partner, "start": getdate(start), "end": getdate(end)}
	if company:
		filters.append("si.company = %(company)s")
		params["company"] = company

	# Pull invoices that received payment in the window.  The join collapses
	# multiple GL credits into a single row per SI; we re-apply the formula
	# in Python so the math stays readable.
	rows = frappe.db.sql(
		f"""
		SELECT
			si.name                                AS sales_invoice,
			si.base_grand_total                    AS invoice_total,
			COALESCE(si.amount_eligible_for_commission, 0) AS eligible_amount,
			SUM(gle.credit)                        AS paid_in_window
		FROM `tabSales Invoice` si
		INNER JOIN `tabGL Entry` gle
			ON gle.against_voucher_type = 'Sales Invoice'
		   AND gle.against_voucher     = si.name
		   AND gle.account             = si.debit_to
		   AND gle.is_cancelled        = 0
		   AND gle.credit              > 0
		   AND gle.posting_date BETWEEN %(start)s AND %(end)s
		WHERE {' AND '.join(filters)}
		GROUP BY si.name, si.base_grand_total, si.amount_eligible_for_commission
		HAVING paid_in_window > 0
		""",
		params,
		as_dict=True,
	)

	total_commission = 0.0
	total_paid = 0.0
	breakdown = []
	for row in rows:
		invoice_total = flt(row.invoice_total)
		eligible = flt(row.eligible_amount)
		paid = flt(row.paid_in_window)
		if invoice_total <= 0 or eligible <= 0:
			continue
		commission = flt((rate / 100.0) * eligible * (paid / invoice_total), 2)
		total_commission += commission
		total_paid += paid
		breakdown.append({
			"sales_invoice": row.sales_invoice,
			"invoice_total": invoice_total,
			"eligible_amount": eligible,
			"paid_in_window": paid,
			"rate": rate,
			"commission": commission,
		})

	return {
		"total_commission": flt(total_commission, 2),
		"total_paid": flt(total_paid, 2),
		"rows": breakdown,
	}


def compute_partner_commission(partner: str, start, end, company: str | None = None) -> float:
	return get_partner_commission_breakdown(partner, start, end, company)["total_commission"]


def compute_employee_commission(employee: str, start, end, company: str | None = None) -> float:
	"""Sum commission across every Sales Partner linked to `employee`."""
	partners = frappe.get_all("Sales Partner", filters={"custom_employee": employee}, pluck="name")
	if not partners:
		return 0.0
	total = 0.0
	for partner in partners:
		total += compute_partner_commission(partner, start, end, company)
	return flt(total, 2)

"""
Sales Partner Commission Ledger — payment-based view.

Shows every receivable-account credit posted against a Sales Invoice that
carries a Sales Partner, with the commission earned for that specific
payment computed using:

    commission = (rate / 100)
                 * amount_eligible_for_commission
                 * (credit / base_grand_total)

This replaces the old JE-based ledger now that commission is no longer
booked per payment.  The "Journal Entry" column has been replaced with
"Payment Voucher" — i.e. the Payment Entry / Journal Entry that triggered
the credit.
"""

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
	filters = filters or {}
	return get_columns(), get_data(filters)


def get_columns():
	return [
		{"label": _("Posting Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 110},
		{"label": _("Sales Partner"), "fieldname": "sales_partner", "fieldtype": "Link", "options": "Sales Partner", "width": 160},
		{"label": _("Sales Invoice"), "fieldname": "sales_invoice", "fieldtype": "Link", "options": "Sales Invoice", "width": 160},
		{"label": _("SI Date"), "fieldname": "si_date", "fieldtype": "Date", "width": 110},
		{"label": _("Invoice Total"), "fieldname": "invoice_total", "fieldtype": "Currency", "width": 130},
		{"label": _("Eligible Amount"), "fieldname": "eligible_amount", "fieldtype": "Currency", "width": 130},
		{"label": _("Paid"), "fieldname": "paid", "fieldtype": "Currency", "width": 120},
		{"label": _("Rate %"), "fieldname": "rate", "fieldtype": "Float", "width": 80, "precision": 4},
		{"label": _("Commission"), "fieldname": "commission", "fieldtype": "Currency", "width": 130},
		{"label": _("Payment Voucher"), "fieldname": "payment_voucher", "fieldtype": "Dynamic Link", "options": "payment_voucher_type", "width": 160},
		{"label": _("Voucher Type"), "fieldname": "payment_voucher_type", "fieldtype": "Data", "width": 120},
	]


def get_data(filters):
	conditions = ["gle.is_cancelled = 0", "gle.credit > 0", "gle.against_voucher_type = 'Sales Invoice'", "si.docstatus = 1", "si.is_return = 0", "si.sales_partner IS NOT NULL", "si.sales_partner != ''"]
	params: dict = {}
	if filters.get("from_date"):
		conditions.append("gle.posting_date >= %(from_date)s")
		params["from_date"] = filters["from_date"]
	if filters.get("to_date"):
		conditions.append("gle.posting_date <= %(to_date)s")
		params["to_date"] = filters["to_date"]
	if filters.get("company"):
		conditions.append("gle.company = %(company)s")
		params["company"] = filters["company"]
	if filters.get("sales_partner"):
		conditions.append("si.sales_partner = %(sales_partner)s")
		params["sales_partner"] = filters["sales_partner"]

	rows = frappe.db.sql(
		f"""
		SELECT
			gle.posting_date,
			gle.credit                              AS paid,
			gle.voucher_type                        AS payment_voucher_type,
			gle.voucher_no                          AS payment_voucher,
			si.name                                 AS sales_invoice,
			si.posting_date                         AS si_date,
			si.sales_partner                        AS sales_partner,
			si.base_grand_total                     AS invoice_total,
			COALESCE(si.amount_eligible_for_commission, 0) AS eligible_amount,
			COALESCE(sp.commission_rate, 0)         AS rate
		FROM `tabGL Entry` gle
		INNER JOIN `tabSales Invoice` si
			ON si.name = gle.against_voucher
		   AND gle.account = si.debit_to
		INNER JOIN `tabSales Partner` sp
			ON sp.name = si.sales_partner
		WHERE {' AND '.join(conditions)}
		ORDER BY gle.posting_date, gle.voucher_no
		""",
		params,
		as_dict=True,
	)

	data = []
	for r in rows:
		invoice_total = flt(r.invoice_total)
		eligible = flt(r.eligible_amount)
		paid = flt(r.paid)
		rate = flt(r.rate)
		commission = 0.0
		if invoice_total > 0 and eligible > 0 and rate > 0:
			commission = flt((rate / 100.0) * eligible * (paid / invoice_total), 2)
		r["commission"] = commission
		data.append(r)
	return data

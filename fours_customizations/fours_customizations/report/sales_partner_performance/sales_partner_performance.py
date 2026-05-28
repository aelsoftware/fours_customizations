import frappe
from frappe import _


def execute(filters=None):
	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns():
	return [
		{
			"label": _("Sales Partner"),
			"fieldname": "sales_partner",
			"fieldtype": "Link",
			"options": "Sales Partner",
			"width": 180,
		},
		{
			"label": _("Total Sales"),
			"fieldname": "total_sales",
			"fieldtype": "Currency",
			"width": 140,
		},
		{
			"label": _("Paid"),
			"fieldname": "paid",
			"fieldtype": "Currency",
			"width": 140,
		},
		{
			"label": _("Outstanding"),
			"fieldname": "outstanding",
			"fieldtype": "Currency",
			"width": 140,
		},
		{
			"label": _("Recovered Payments"),
			"fieldname": "recovered_payments",
			"fieldtype": "Currency",
			"width": 160,
		},
		{
			"label": _("Total Payments"),
			"fieldname": "total_payments",
			"fieldtype": "Currency",
			"width": 140,
		},
		{
			"label": _("Total Commission"),
			"fieldname": "total_commission",
			"fieldtype": "Currency",
			"width": 150,
		},
	]


def get_data(filters):
	si_data = _get_si_data(filters)
	commission_data = _get_je_commission(filters)
	recovered_data = _get_recovered_payments(filters)

	all_partners = set(si_data.keys()) | set(commission_data.keys()) | set(recovered_data.keys())

	data = []
	for partner in sorted(all_partners):
		si = si_data.get(partner, {})
		total_sales = (si.get("total_sales") or 0)
		paid = (si.get("paid") or 0)
		outstanding = (si.get("outstanding") or 0)
		recovered = (recovered_data.get(partner) or 0)
		commission = (commission_data.get(partner) or 0)

		data.append({
			"sales_partner": partner,
			"total_sales": total_sales,
			"paid": paid,
			"outstanding": outstanding,
			"recovered_payments": recovered,
			"total_payments": paid + recovered,
			"total_commission": commission,
		})

	return data


def _get_si_data(filters):
	"""Sales Invoice totals grouped by sales partner (excludes credit notes)."""
	conditions = ""
	if filters.get("from_date"):
		conditions += " AND si.posting_date >= %(from_date)s"
	if filters.get("to_date"):
		conditions += " AND si.posting_date <= %(to_date)s"
	if filters.get("company"):
		conditions += " AND si.company = %(company)s"
	if filters.get("sales_partner"):
		conditions += " AND si.sales_partner = %(sales_partner)s"

	result = frappe.db.sql(
		"""
		SELECT
			si.sales_partner,
			SUM(si.base_grand_total) AS total_sales,
			SUM(si.base_grand_total - si.outstanding_amount) AS paid,
			SUM(si.outstanding_amount) AS outstanding
		FROM `tabSales Invoice` si
		WHERE si.docstatus = 1
			AND si.is_return = 0
			AND si.sales_partner IS NOT NULL
			AND si.sales_partner != ''
			{conditions}
		GROUP BY si.sales_partner
		""".format(conditions=conditions),
		filters,
		as_dict=True,
	)

	return {row.sales_partner: row for row in result}


def _get_je_commission(filters):
	"""Commission earned, computed from GL payment credits and the standard
	`amount_eligible_for_commission` field on each Sales Invoice.

	Formula:
	    commission = (rate / 100) * eligible * (paid / base_grand_total)
	summed per Sales Partner across every payment in the window.
	"""
	from frappe.utils import flt

	conditions = ["gle.is_cancelled = 0", "gle.credit > 0", "gle.against_voucher_type = 'Sales Invoice'", "si.docstatus = 1", "si.is_return = 0", "si.sales_partner IS NOT NULL", "si.sales_partner != ''"]
	if filters.get("from_date"):
		conditions.append("gle.posting_date >= %(from_date)s")
	if filters.get("to_date"):
		conditions.append("gle.posting_date <= %(to_date)s")
	if filters.get("company"):
		conditions.append("gle.company = %(company)s")
	if filters.get("sales_partner"):
		conditions.append("si.sales_partner = %(sales_partner)s")

	rows = frappe.db.sql(
		f"""
		SELECT
			si.sales_partner,
			si.base_grand_total                          AS invoice_total,
			COALESCE(si.amount_eligible_for_commission, 0) AS eligible,
			COALESCE(sp.commission_rate, 0)              AS rate,
			SUM(gle.credit)                              AS paid
		FROM `tabGL Entry` gle
		INNER JOIN `tabSales Invoice` si
			ON si.name = gle.against_voucher
		   AND gle.account = si.debit_to
		INNER JOIN `tabSales Partner` sp
			ON sp.name = si.sales_partner
		WHERE {' AND '.join(conditions)}
		GROUP BY si.name, si.sales_partner, si.base_grand_total, si.amount_eligible_for_commission, sp.commission_rate
		""",
		filters,
		as_dict=True,
	)

	totals: dict[str, float] = {}
	for r in rows:
		invoice_total = flt(r.invoice_total)
		eligible = flt(r.eligible)
		paid = flt(r.paid)
		rate = flt(r.rate)
		if invoice_total <= 0 or eligible <= 0 or rate <= 0:
			continue
		commission = (rate / 100.0) * eligible * (paid / invoice_total)
		totals[r.sales_partner] = totals.get(r.sales_partner, 0.0) + commission
	return totals


def _get_recovered_payments(filters):
	"""Payments made in the date range against SIs from before the date range."""
	conditions = ""
	if filters.get("from_date"):
		conditions += " AND pe.posting_date >= %(from_date)s"
		conditions += " AND si.posting_date < %(from_date)s"
	if filters.get("to_date"):
		conditions += " AND pe.posting_date <= %(to_date)s"
	if filters.get("company"):
		conditions += " AND pe.company = %(company)s"
	if filters.get("sales_partner"):
		conditions += " AND si.sales_partner = %(sales_partner)s"

	result = frappe.db.sql(
		"""
		SELECT
			si.sales_partner,
			SUM(per.allocated_amount) AS recovered
		FROM `tabPayment Entry Reference` per
		INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
		INNER JOIN `tabSales Invoice` si ON si.name = per.reference_name
		WHERE pe.docstatus = 1
			AND per.reference_doctype = 'Sales Invoice'
			AND si.is_return = 0
			AND si.sales_partner IS NOT NULL
			AND si.sales_partner != ''
			{conditions}
		GROUP BY si.sales_partner
		""".format(conditions=conditions),
		filters,
		as_dict=True,
	)

	return {row.sales_partner: row.recovered for row in result}

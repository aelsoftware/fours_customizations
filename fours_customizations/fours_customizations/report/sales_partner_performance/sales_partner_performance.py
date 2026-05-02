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
	"""Net commission from commission JEs (matches Accounts Payable).

	Credits on the supplier/creditor account = positive commission.
	Debits (reversals, credit notes) = negative commission.
	"""
	conditions = ""
	if filters.get("from_date"):
		conditions += " AND je.posting_date >= %(from_date)s"
	if filters.get("to_date"):
		conditions += " AND je.posting_date <= %(to_date)s"
	if filters.get("company"):
		conditions += " AND je.company = %(company)s"
	if filters.get("sales_partner"):
		conditions += " AND si.sales_partner = %(sales_partner)s"

	result = frappe.db.sql(
		"""
		SELECT
			si.sales_partner,
			SUM(jea.credit_in_account_currency - jea.debit_in_account_currency) AS net_commission
		FROM `tabJournal Entry` je
		INNER JOIN `tabJournal Entry Account` jea
			ON jea.parent = je.name AND jea.party_type = 'Supplier'
		INNER JOIN `tabSales Invoice` si
			ON si.name = je.custom_commission_sales_invoice
		WHERE je.docstatus = 1
			AND je.custom_commission_sales_invoice IS NOT NULL
			AND je.custom_commission_sales_invoice != ''
			{conditions}
		GROUP BY si.sales_partner
		""".format(conditions=conditions),
		filters,
		as_dict=True,
	)

	return {row.sales_partner: row.net_commission for row in result}


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

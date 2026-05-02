import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns():
	return [
		{
			"label": _("Date"),
			"fieldname": "posting_date",
			"fieldtype": "Date",
			"width": 110,
		},
		{
			"label": _("Sales Partner"),
			"fieldname": "sales_partner",
			"fieldtype": "Link",
			"options": "Sales Partner",
			"width": 160,
		},
		{
			"label": _("Commission Amount"),
			"fieldname": "commission_amount",
			"fieldtype": "Currency",
			"width": 150,
		},
		{
			"label": _("Type"),
			"fieldname": "entry_type",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("Journal Entry"),
			"fieldname": "journal_entry",
			"fieldtype": "Link",
			"options": "Journal Entry",
			"width": 160,
		},
		{
			"label": _("Sales Invoice"),
			"fieldname": "sales_invoice",
			"fieldtype": "Link",
			"options": "Sales Invoice",
			"width": 160,
		},
		{
			"label": _("SI Date"),
			"fieldname": "si_date",
			"fieldtype": "Date",
			"width": 110,
		},
		{
			"label": _("Payment Entry"),
			"fieldname": "payment_entry",
			"fieldtype": "Link",
			"options": "Payment Entry",
			"width": 160,
		},
		{
			"label": _("PE Date"),
			"fieldname": "pe_date",
			"fieldtype": "Date",
			"width": 110,
		},
		{
			"label": _("Remarks"),
			"fieldname": "remarks",
			"fieldtype": "Data",
			"width": 300,
		},
	]


def get_data(filters):
	conditions = ""
	if filters.get("from_date"):
		conditions += " AND je.posting_date >= %(from_date)s"
	if filters.get("to_date"):
		conditions += " AND je.posting_date <= %(to_date)s"
	if filters.get("company"):
		conditions += " AND je.company = %(company)s"

	# Fetch all commission JEs (both original and reversals)
	journal_entries = frappe.db.sql(
		"""
		SELECT
			je.name,
			je.posting_date,
			je.custom_commission_sales_invoice,
			je.custom_commission_payment_entry,
			je.custom_commission_voucher_no,
			je.user_remark
		FROM `tabJournal Entry` je
		WHERE je.docstatus = 1
			AND je.custom_commission_sales_invoice IS NOT NULL
			AND je.custom_commission_sales_invoice != ''
			{conditions}
		ORDER BY je.posting_date, je.name
		""".format(conditions=conditions),
		filters,
		as_dict=True,
	)

	if not journal_entries:
		return []

	# Collect all SI and PE names for batch lookup
	si_names = set()
	pe_names = set()
	for je in journal_entries:
		if je.custom_commission_sales_invoice:
			si_names.add(je.custom_commission_sales_invoice)
		if je.custom_commission_payment_entry:
			pe_names.add(je.custom_commission_payment_entry)

	# Batch fetch SI details
	si_map = {}
	if si_names:
		si_data = frappe.db.sql(
			"""
			SELECT name, posting_date, sales_partner, is_return
			FROM `tabSales Invoice`
			WHERE name IN %s
			""",
			[list(si_names)],
			as_dict=True,
		)
		si_map = {s.name: s for s in si_data}

	# Batch fetch PE dates
	pe_map = {}
	if pe_names:
		pe_data = frappe.db.sql(
			"""
			SELECT name, posting_date
			FROM `tabPayment Entry`
			WHERE name IN %s
			""",
			[list(pe_names)],
			as_dict=True,
		)
		pe_map = {p.name: p for p in pe_data}

	# Get commission amounts from JE account rows
	je_names = [je.name for je in journal_entries]
	account_rows = frappe.db.sql(
		"""
		SELECT
			parent,
			debit_in_account_currency,
			credit_in_account_currency
		FROM `tabJournal Entry Account`
		WHERE parent IN %s
			AND debit_in_account_currency > 0
		ORDER BY parent
		""",
		[je_names],
		as_dict=True,
	)
	# Map JE name → first debit amount (the commission/reversal amount)
	je_debit_map = {}
	for row in account_rows:
		if row.parent not in je_debit_map:
			je_debit_map[row.parent] = flt(row.debit_in_account_currency)

	# Filter by sales partner if specified
	partner_filter = filters.get("sales_partner")

	data = []
	for je in journal_entries:
		si_name = je.custom_commission_sales_invoice
		pe_name = je.custom_commission_payment_entry
		voucher_no = je.custom_commission_voucher_no or ""

		si_info = si_map.get(si_name, {})
		pe_info = pe_map.get(pe_name, {})

		sales_partner = si_info.get("sales_partner", "")
		if partner_filter and sales_partner != partner_filter:
			continue

		# Determine type and sign
		is_reversal = voucher_no.startswith("REV-")
		is_credit_note = si_info.get("is_return", 0)

		debit_amount = je_debit_map.get(je.name, 0)

		if is_reversal:
			entry_type = "Reversal"
			commission_amount = -1 * debit_amount
		elif is_credit_note:
			entry_type = "Credit Note"
			commission_amount = -1 * debit_amount
		else:
			entry_type = "Commission"
			commission_amount = debit_amount

		data.append({
			"posting_date": je.posting_date,
			"sales_partner": sales_partner,
			"commission_amount": commission_amount,
			"entry_type": entry_type,
			"journal_entry": je.name,
			"sales_invoice": si_name,
			"si_date": si_info.get("posting_date"),
			"payment_entry": pe_name,
			"pe_date": pe_info.get("posting_date"),
			"remarks": je.user_remark,
		})

	return data

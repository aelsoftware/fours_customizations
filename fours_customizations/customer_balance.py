import frappe
from frappe.utils import flt


@frappe.whitelist()
def customer(customer, company=None):
	"""Return a customer's outstanding balance in their billing currency.

	Ported from the saleslive app so the Account Balance field on Sales Invoice /
	Sales Order keeps working after saleslive is uninstalled.
	Returns e.g. {"formatted": "UGX 10,000 DR", ...}
	"""
	if not company:
		company = frappe.defaults.get_user_default("Company")

	currency = frappe.get_cached_value("Customer", customer, "default_currency")
	if not currency:
		currency = frappe.get_cached_value("Company", company, "default_currency")

	rows = frappe.db.sql(
		"""
		SELECT SUM(debit_in_account_currency) - SUM(credit_in_account_currency)
		FROM `tabGL Entry`
		WHERE party_type = 'Customer'
		  AND party = %s
		  AND company = %s
		  AND account_currency = %s
		  AND ifnull(is_cancelled, 0) = 0
		""",
		(customer, company, currency),
	)
	balance = flt(rows[0][0]) if rows and rows[0][0] else 0.0

	if balance > 0:
		balance_type = "DR"
	elif balance < 0:
		balance_type = "CR"
		balance = abs(balance)
	else:
		balance_type = "Settled"

	return {
		"customer": customer,
		"company": company,
		"currency": currency,
		"balance": balance,
		"formatted": f"{frappe.utils.fmt_money(balance, currency=currency)} {balance_type}",
	}

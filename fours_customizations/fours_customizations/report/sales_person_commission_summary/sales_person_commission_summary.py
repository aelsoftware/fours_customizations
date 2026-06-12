"""
Sales Person Commission Summary
================================

One row per Sales Person, with:

  • Total Sales       — Sales Invoices where `custom_sales_person` = this person
                        and posting_date in the window.
  • POS Refunds       — amounts refunded to customers on POS returns; these
                        subtract from the commission base.
  • POS Payments      — amounts collected on POS invoices (`is_pos = 1`) owned
                        by this person (no Payment Entry exists for those),
                        net of POS refunds.
  • PE Refunds        — Payment Entries of type "Pay" to a Customer with this
                        person as `sales_person`; subtract from the base.
  • Total Payments    — incoming Payment Entries where `sales_person` = this
                        person, less PE refunds, plus the net POS payments.
  • Commission Rate   — from the Sales Person doctype.
  • Total Commission  — total_payments × commission_rate / 100.

Filters: From Date, To Date, Company, Sales Person.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate, get_first_day, get_last_day, today

from fours_customizations.commission_handler import get_sales_person_summary


def execute(filters=None):
	filters = _normalise(filters or {})
	return _columns(), _data(filters)


def _normalise(filters: dict) -> dict:
	today_d = getdate(today())
	if not filters.get("from_date"):
		filters["from_date"] = get_first_day(today_d)
	if not filters.get("to_date"):
		filters["to_date"] = get_last_day(today_d)
	return filters


def _columns() -> list[dict]:
	return [
		{
			"label": _("Sales Person"),
			"fieldname": "sales_person",
			"fieldtype": "Link",
			"options": "Sales Person",
			"width": 220,
		},
		{
			"label": _("Employee"),
			"fieldname": "employee",
			"fieldtype": "Link",
			"options": "Employee",
			"width": 180,
		},
		{
			"label": _("Total Sales"),
			"fieldname": "total_sales",
			"fieldtype": "Currency",
			"width": 160,
		},
		{
			"label": _("POS Refunds"),
			"fieldname": "pos_refunds",
			"fieldtype": "Currency",
			"width": 160,
		},
		{
			"label": _("POS Payments (Net)"),
			"fieldname": "pos_payments",
			"fieldtype": "Currency",
			"width": 160,
		},
		{
			"label": _("PE Refunds"),
			"fieldname": "payment_entry_refunds",
			"fieldtype": "Currency",
			"width": 160,
		},
		{
			"label": _("Total Payments"),
			"fieldname": "total_payments",
			"fieldtype": "Currency",
			"width": 160,
		},
		{
			"label": _("Commission Rate (%)"),
			"fieldname": "commission_rate",
			"fieldtype": "Float",
			"precision": 4,
			"width": 140,
		},
		{
			"label": _("Total Commission"),
			"fieldname": "total_commission",
			"fieldtype": "Currency",
			"width": 160,
		},
	]


def _data(filters: dict) -> list[dict]:
	person_filters = {"enabled": 1}
	if filters.get("sales_person"):
		person_filters["name"] = filters["sales_person"]

	persons = frappe.get_all(
		"Sales Person",
		filters=person_filters,
		fields=["name", "employee"],
		order_by="name",
	)

	rows = []
	for p in persons:
		summary = get_sales_person_summary(
			p["name"],
			filters["from_date"],
			filters["to_date"],
			filters.get("company"),
		)
		# Skip Sales Persons with absolutely no activity to keep the report tidy.
		if (
			flt(summary["total_sales"]) == 0
			and flt(summary["total_payments"]) == 0
			and flt(summary["total_commission"]) == 0
		):
			continue
		rows.append({
			"sales_person": p["name"],
			"employee": p.get("employee"),
			"total_sales": summary["total_sales"],
			"pos_refunds": summary.get("pos_refunds", 0),
			"pos_payments": summary.get("pos_payments", 0),
			"payment_entry_refunds": summary.get("payment_entry_refunds", 0),
			"total_payments": summary["total_payments"],
			"commission_rate": summary["commission_rate"],
			"total_commission": summary["total_commission"],
		})
	return rows

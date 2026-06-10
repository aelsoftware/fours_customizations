"""
Salary Slip Handler — Fours Customizations
==========================================

Hook: Salary Slip — before_save / before_insert

  • Adds attendance-based deductions (absent, late, early exit, no checkout)
    using rates configured on the employee's Designation.
  • Adds designation-based overtime as a "Designation Overtime Pay" earning.
  • Adds Sales Commission earnings for any Sales Partner linked to the
    employee, calculated across the salary period (see commission_handler.py).
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, rounded

from fours_customizations.commission_handler import compute_employee_commission
from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_setting,
)


def calculate_and_add_deductions(doc, method=None):
	"""Add attendance deductions, overtime, and commission to the salary slip."""
	if doc.docstatus != 0:
		return
	if not doc.employee or not doc.start_date or not doc.end_date:
		return
	if not doc.earnings:
		return
	if getattr(doc, "_4s_calculated", False):
		return
	doc._4s_calculated = True

	try:
		employee = frappe.get_cached_doc("Employee", doc.employee)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S Salary Slip: employee load failed")
		return

	if employee.designation:
		try:
			designation = frappe.get_cached_doc("Designation", employee.designation)
			_apply_attendance_deductions(doc, designation)
			_apply_overtime(doc, designation)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "4S Salary Slip: designation load failed")

	commission = _apply_commission(doc)

	# Summary fields for print/reporting (custom fields on Salary Slip).
	doc.custom_total_commission = flt(commission)
	doc.custom_basic_pay = _get_employee_base(doc)

	doc.gross_pay = sum([flt(e.amount) for e in doc.earnings])
	doc.total_deduction = sum([flt(d.amount) for d in doc.deductions])
	doc.net_pay = doc.gross_pay - doc.total_deduction
	_sync_derived_totals(doc)


# ── attendance ──────────────────────────────────────────────────────────────

def _apply_attendance_deductions(doc, designation):
	records = frappe.get_all(
		"Attendance",
		filters={
			"employee": doc.employee,
			"attendance_date": ["between", [doc.start_date, doc.end_date]],
			"docstatus": 1,
		},
		fields=["name", "status", "in_time", "out_time", "late_entry", "early_exit"],
	)

	absent = late = early = no_co = 0
	for att in records:
		if att.status == "Absent":
			absent += 1
		if att.late_entry == 1:
			late += 1
		if att.early_exit == 1:
			early += 1
		if att.status in ("Present", "Half Day") and not att.out_time:
			no_co += 1

	mapping = {
		"Absent Deduction": absent * flt(designation.absent_deduction or 0),
		"Late Deduction": late * flt(designation.late_deduction or 0),
		"Early Exit Deduction": early * flt(designation.early_exit_deduction or 0),
		"No Checkout Deduction": no_co * flt(designation.no_checkout_deduction or 0),
	}

	for component, amount in mapping.items():
		if amount <= 0:
			continue
		_upsert(doc.deductions, component, amount, doc, "deductions")


# ── overtime ────────────────────────────────────────────────────────────────

def _apply_overtime(doc, designation):
	if not designation.overtime_start_time:
		return
	from fours_customizations.overtime_utils import calculate_designation_overtime

	data = calculate_designation_overtime(doc.employee, doc.start_date, doc.end_date)
	amount = flt(data.get("total_amount", 0))
	if amount <= 0:
		return
	_upsert(doc.earnings, "Designation Overtime Pay", amount, doc, "earnings")


# ── commission ──────────────────────────────────────────────────────────────

def _apply_commission(doc):
	"""Add the commission earning. Returns the commission amount (0 if none)."""
	commission_component = get_setting("commission_salary_component", "Sales Commission")
	if not commission_component:
		return 0.0
	if not frappe.db.exists("Salary Component", commission_component):
		return 0.0
	amount = compute_employee_commission(doc.employee, doc.start_date, doc.end_date, doc.company)
	if amount <= 0:
		return 0.0
	_upsert(doc.earnings, commission_component, amount, doc, "earnings")
	return flt(amount)


# ── helpers ─────────────────────────────────────────────────────────────────

def _get_employee_base(doc):
	"""Employee's `base` from the latest Salary Structure Assignment in effect."""
	base = frappe.db.get_value(
		"Salary Structure Assignment",
		{"employee": doc.employee, "docstatus": 1, "from_date": ("<=", doc.end_date)},
		"base",
		order_by="from_date desc",
	)
	return flt(base)


def _sync_derived_totals(doc):
	"""Keep company-currency / rounded / in-words fields consistent with the
	totals we just recomputed — the standard calculation ran before our rows
	were added, so these would otherwise stay at the pre-commission values."""
	exchange_rate = flt(doc.exchange_rate) or 1
	doc.base_gross_pay = flt(flt(doc.gross_pay) * exchange_rate, doc.precision("base_gross_pay"))
	doc.base_total_deduction = flt(
		flt(doc.total_deduction) * exchange_rate, doc.precision("base_total_deduction")
	)
	doc.rounded_total = rounded(flt(doc.net_pay))
	doc.base_net_pay = flt(flt(doc.net_pay) * exchange_rate, doc.precision("base_net_pay"))
	doc.base_rounded_total = rounded(flt(doc.base_net_pay))
	try:
		doc.set_net_total_in_words()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S Salary Slip: in-words update failed")


def _upsert(rows, component_name, amount, doc, table):
	for row in rows:
		if row.salary_component == component_name:
			row.amount = amount
			return
	doc.append(table, {"salary_component": component_name, "amount": amount})


def get_attendance_summary(employee, start_date, end_date):
	"""Helper used by reports / scripts. Returns a violation summary."""
	emp = frappe.get_cached_doc("Employee", employee)
	if not emp.designation:
		return {"error": "Employee has no designation"}

	designation = frappe.get_cached_doc("Designation", emp.designation)

	records = frappe.get_all(
		"Attendance",
		filters={
			"employee": employee,
			"attendance_date": ["between", [start_date, end_date]],
			"docstatus": 1,
		},
		fields=["name", "attendance_date", "status", "in_time", "out_time", "late_entry", "early_exit"],
	)

	v = {
		"absent": {"count": 0, "rate": flt(designation.absent_deduction or 0), "dates": []},
		"late": {"count": 0, "rate": flt(designation.late_deduction or 0), "dates": []},
		"early_exit": {"count": 0, "rate": flt(designation.early_exit_deduction or 0), "dates": []},
		"no_checkout": {"count": 0, "rate": flt(designation.no_checkout_deduction or 0), "dates": []},
	}
	for att in records:
		if att.status == "Absent":
			v["absent"]["count"] += 1
			v["absent"]["dates"].append(att.attendance_date)
		if att.late_entry == 1:
			v["late"]["count"] += 1
			v["late"]["dates"].append(att.attendance_date)
		if att.early_exit == 1:
			v["early_exit"]["count"] += 1
			v["early_exit"]["dates"].append(att.attendance_date)
		if att.status in ("Present", "Half Day") and not att.out_time:
			v["no_checkout"]["count"] += 1
			v["no_checkout"]["dates"].append(att.attendance_date)

	for key in v:
		v[key]["amount"] = v[key]["count"] * v[key]["rate"]

	total = sum(item["amount"] for item in v.values())
	return {
		"employee": employee,
		"employee_name": emp.employee_name,
		"designation": emp.designation,
		"period": f"{start_date} to {end_date}",
		"violations": v,
		"total_deductions": total,
	}

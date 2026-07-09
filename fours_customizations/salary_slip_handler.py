"""
Salary Slip Handler — Fours Customizations
==========================================

Hook: Salary Slip — before_save / before_insert

  • Adds attendance-based deductions (absent, late, early exit, no checkout)
    using rates configured on the employee's Designation.
  • Adds submitted Employee Salary Deduction amounts dated inside the salary
    period (grouped by reason → Salary Component).
  • Adds designation-based overtime as a "Designation Overtime Pay" earning.
  • Adds Sales Commission earnings for any Sales Partner linked to the
    employee, calculated across the salary period (see commission_handler.py).
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import flt, getdate, rounded

from fours_customizations.commission_handler import compute_employee_commission
from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_setting,
)


def calculate_and_add_deductions(doc, method=None):
	"""Add attendance deductions, overtime, and commission to the salary slip.

	Runs on draft saves AND at submit time. ERPNext re-runs its own
	calculate_net_pay during the submit-time validate, which rebuilds the
	earnings/deductions tables and drops the attendance / overtime / commission
	rows we add while the slip is a draft. Re-applying at submit (docstatus 1)
	keeps them on the final, submitted slip — otherwise the accruals posted on
	submit omit every attendance deduction. Cancelled/amended states are skipped.
	"""
	if doc.docstatus not in (0, 1):
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

	try:
		_apply_employee_salary_deductions(doc)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S Salary Slip: salary deduction apply failed")

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

_ATTENDANCE_COMPONENTS = (
	"Absent Deduction",
	"Late Deduction",
	"Early Exit Deduction",
	"No Checkout Deduction",
)


def _apply_attendance_deductions(doc, designation):
	"""Attendance deductions, gated by shift assignment and holidays.

	Only days on which the employee has a submitted Shift Assignment AND which
	are not holidays count. Employees with no shift assignment in the period
	(e.g. salaried staff who never clock in) are never deducted. Idempotent:
	components drop to zero — and their rows are removed — when nothing qualifies,
	so a recompute (or a corrected shift/holiday setup) self-heals.
	"""
	start, end = getdate(doc.start_date), getdate(doc.end_date)
	eligible_dates = _shift_assigned_dates(doc.employee, start, end) - _holiday_dates(
		doc.employee, doc.company, start, end
	)

	absent = late = early = no_co = 0
	if eligible_dates:
		records = frappe.get_all(
			"Attendance",
			filters={
				"employee": doc.employee,
				"attendance_date": ["between", [start, end]],
				"docstatus": 1,
			},
			fields=["name", "status", "attendance_date", "out_time", "late_entry", "early_exit"],
		)
		for att in records:
			if getdate(att.attendance_date) not in eligible_dates:
				continue
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

	for component in _ATTENDANCE_COMPONENTS:
		amount = flt(mapping.get(component))
		if amount > 0:
			_upsert(doc.deductions, component, amount, doc, "deductions")
		else:
			_remove_component_row(doc, "deductions", component)


def _shift_assigned_dates(employee, start, end) -> set:
	"""Set of dates in [start, end] on which the employee has a submitted Shift
	Assignment. Handles both per-day rows and open-ended / ranged assignments
	(a blank end_date is treated as ongoing)."""
	rows = frappe.get_all(
		"Shift Assignment",
		filters={
			"employee": employee,
			"docstatus": 1,
			"start_date": ["<=", end],
		},
		fields=["start_date", "end_date"],
	)
	dates: set = set()
	for row in rows:
		s = max(getdate(row.start_date), start)
		e = min(getdate(row.end_date) if row.end_date else end, end)
		day = s
		while day <= e:
			dates.add(day)
			day += timedelta(days=1)
	return dates


def _holiday_dates(employee, company, start, end) -> set:
	"""Holiday dates in [start, end] from the employee's Holiday List, falling
	back to the company default."""
	holiday_list = frappe.db.get_value("Employee", employee, "holiday_list") or (
		frappe.db.get_value("Company", company, "default_holiday_list") if company else None
	)
	if not holiday_list:
		return set()
	rows = frappe.get_all(
		"Holiday",
		filters={"parent": holiday_list, "holiday_date": ["between", [start, end]]},
		fields=["holiday_date"],
	)
	return {getdate(r.holiday_date) for r in rows}


# ── employee salary deductions ──────────────────────────────────────────────

_ESD_FALLBACK_COMPONENT = "Employee Salary Deduction"


def _esd_component(reason):
	"""Salary Component for an Employee Salary Deduction reason — the reason
	itself when a matching component exists, else the generic fallback."""
	reason = (reason or "").strip()
	if reason and frappe.db.exists("Salary Component", reason):
		return reason
	return _ESD_FALLBACK_COMPONENT


def _apply_employee_salary_deductions(doc):
	"""Pull submitted Employee Salary Deductions dated inside the salary period
	into the slip's deductions table, grouped by reason → Salary Component.

	Cancelled deductions are included in the candidate set (with zero amount)
	so a row added earlier is removed again when its deduction is cancelled.
	"""
	if not frappe.db.exists("DocType", "Employee Salary Deduction"):
		return

	rows = frappe.get_all(
		"Employee Salary Deduction",
		filters={
			"employee": doc.employee,
			"docstatus": ["in", [1, 2]],
			"date": ["between", [f"{doc.start_date} 00:00:00", f"{doc.end_date} 23:59:59"]],
		},
		fields=["reason", "amount", "docstatus"],
	)

	totals: dict[str, float] = {}
	candidates: set[str] = set()
	for row in rows:
		component = _esd_component(row.reason)
		candidates.add(component)
		if row.docstatus == 1:
			totals[component] = totals.get(component, 0.0) + flt(row.amount)

	for component in candidates:
		amount = flt(totals.get(component))
		if amount > 0:
			_upsert(doc.deductions, component, amount, doc, "deductions")
		else:
			_remove_component_row(doc, "deductions", component)


def _remove_component_row(doc, table, component_name):
	rows = [r for r in doc.get(table) or [] if r.salary_component != component_name]
	if len(rows) != len(doc.get(table) or []):
		doc.set(table, rows)


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

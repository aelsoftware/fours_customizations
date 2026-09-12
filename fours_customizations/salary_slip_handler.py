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
from frappe.utils import flt, getdate
from hrms.utils.holiday_list import (
	get_holiday_dates_between_range,
	get_holiday_list_for_employee,
)

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

	_validate_holiday_list_coverage(doc)

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
	_cap_attendance_deductions(doc)

	# Summary fields for print/reporting (custom fields on Salary Slip).
	doc.custom_total_commission = flt(commission)
	doc.custom_basic_pay = _get_component_amount(doc.earnings, "Basic Salary")

	_sync_derived_totals(doc)


# ── attendance ──────────────────────────────────────────────────────────────

_ATTENDANCE_COMPONENTS = (
	"Absent Deduction",
	"Late Deduction",
	"Early Exit Deduction",
	"No Checkout Deduction",
)

# When attendance penalties exceed payable earnings, reduce absence first and
# keep the more specific late / early-exit / no-checkout penalties intact.
_ATTENDANCE_CAP_REDUCTION_ORDER = (
	"Absent Deduction",
	"No Checkout Deduction",
	"Early Exit Deduction",
	"Late Deduction",
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


def _cap_attendance_deductions(doc):
	"""Keep custom attendance penalties from making take-home pay negative.

	Loans, taxes, and other deductions retain their full value. Only the excess
	attendance penalty is reduced, giving payroll the zero floor required by
	HRMS while leaving non-attendance obligations untouched for review.
	"""
	attendance_rows = [
		row
		for row in doc.deductions
		if row.salary_component in _ATTENDANCE_COMPONENTS and not row.do_not_include_in_total
	]
	if not attendance_rows:
		return

	gross_pay = doc.get_component_totals("earnings")
	other_deductions = sum(
		flt(row.amount)
		for row in doc.deductions
		if row.salary_component not in _ATTENDANCE_COMPONENTS and not row.do_not_include_in_total
	)
	available = max(flt(gross_pay) - other_deductions - flt(doc.get("total_loan_repayment")), 0)
	excess = sum(flt(row.amount) for row in attendance_rows) - available
	if excess <= 0:
		return

	for component in _ATTENDANCE_CAP_REDUCTION_ORDER:
		for row in attendance_rows:
			if row.salary_component != component or excess <= 0:
				continue
			reduction = min(flt(row.amount), excess)
			row.amount = flt(flt(row.amount) - reduction, row.precision("amount"))
			excess -= reduction


def _shift_assigned_dates(employee, start, end) -> set:
	"""Set of dates in [start, end] on which the employee has a submitted Shift
	Assignment. Handles both per-day rows and open-ended / ranged assignments
	(a blank end_date is treated as ongoing only while the assignment is Active).
	Date-bounded rows remain historical evidence after HRMS auto-expires them.
	"""
	rows = frappe.get_all(
		"Shift Assignment",
		filters={
			"employee": employee,
			"docstatus": 1,
			"start_date": ["<=", end],
		},
		fields=["start_date", "end_date", "status"],
	)
	dates: set = set()
	for row in rows:
		if row.status == "Inactive" and not row.end_date:
			continue
		s = max(getdate(row.start_date), start)
		e = min(getdate(row.end_date) if row.end_date else end, end)
		day = s
		while day <= e:
			dates.add(day)
			day += timedelta(days=1)
	return dates


def _holiday_dates(employee, company, start, end) -> set:
	"""Holiday dates in [start, end] from effective HRMS assignments.

	The list's configured weekly off remains authoritative even when its dated
	rows have not yet been rolled into a new year. This is the same failsafe used
	by the nightly attendance creator and prevents Sundays becoming deductions.
	"""
	start, end = getdate(start), getdate(end)
	dates = {
		getdate(day)
		for day in get_holiday_dates_between_range(
			employee,
			start,
			end,
			raise_exception_for_holiday_list=False,
		)
	}

	from_info = get_holiday_list_for_employee(
		employee, raise_exception=False, as_on=start, as_dict=True
	) or frappe._dict()
	to_info = get_holiday_list_for_employee(
		employee, raise_exception=False, as_on=end, as_dict=True
	) or frappe._dict()
	legacy_list = frappe.db.get_value("Employee", employee, "holiday_list") or (
		frappe.db.get_value("Company", company, "default_holiday_list") if company else None
	)
	from_list = from_info.get("holiday_list") or legacy_list
	to_list = to_info.get("holiday_list") or from_list
	periods = [(start, end, from_list)]
	if from_list and to_list and from_list != to_list:
		change_date = max(start, getdate(to_info.from_date))
		periods = [(start, change_date - timedelta(days=1), from_list), (change_date, end, to_list)]

	for period_start, period_end, holiday_list in periods:
		if not holiday_list or period_start > period_end:
			continue
		dates.update(
			getdate(row.holiday_date)
			for row in frappe.get_all(
				"Holiday",
				filters={
					"parent": holiday_list,
					"holiday_date": ["between", [period_start, period_end]],
				},
				fields=["holiday_date"],
			)
		)
		weekly_off = frappe.db.get_value("Holiday List", holiday_list, "weekly_off", cache=True)
		day = period_start
		while weekly_off and day <= period_end:
			if day.strftime("%A") == weekly_off:
				dates.add(day)
			day += timedelta(days=1)
	return dates


def _validate_holiday_list_coverage(doc):
	"""Stop payroll rather than silently treating an expired calendar as valid."""
	for boundary in (getdate(doc.start_date), getdate(doc.end_date)):
		holiday_list = get_holiday_list_for_employee(
			doc.employee,
			raise_exception=False,
			as_on=boundary,
		)
		if not holiday_list:
			frappe.throw(
				f"No Holiday List is assigned to {doc.employee} for {boundary}. "
				"Assign one before calculating payroll."
			)
		coverage = frappe.db.get_value(
			"Holiday List", holiday_list, ["from_date", "to_date"], as_dict=True, cache=True
		)
		if not coverage or not (getdate(coverage.from_date) <= boundary <= getdate(coverage.to_date)):
			frappe.throw(
				f"Holiday List '{holiday_list}' does not cover {boundary}. "
				"Extend the list before calculating payroll so weekly offs are not charged as absences."
			)


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
	rows = [
		r
		for r in doc.get(table) or []
		if r.salary_component != component_name or r.additional_salary
	]
	if len(rows) != len(doc.get(table) or []):
		doc.set(table, rows)


# ── overtime ────────────────────────────────────────────────────────────────

def _apply_overtime(doc, designation):
	component = "Designation Overtime Pay"
	if not designation.overtime_start_time:
		_remove_component_row(doc, "earnings", component)
		return
	from fours_customizations.overtime_utils import calculate_designation_overtime

	data = calculate_designation_overtime(doc.employee, doc.start_date, doc.end_date)
	amount = flt(data.get("total_amount", 0))
	if amount <= 0:
		_remove_component_row(doc, "earnings", component)
		return
	_upsert(doc.earnings, component, amount, doc, "earnings")


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
		_remove_component_row(doc, "earnings", commission_component)
		return 0.0
	_upsert(doc.earnings, commission_component, amount, doc, "earnings")
	return flt(amount)


# ── helpers ─────────────────────────────────────────────────────────────────

def _get_component_amount(rows, component_name):
	"""Return the amount actually payable for a component on this slip."""
	for row in rows or []:
		if row.salary_component == component_name:
			return flt(row.amount)
	return 0.0


def _sync_derived_totals(doc):
	"""Re-run HRMS totals after adding custom earnings and deductions.

	The standard calculation runs before this hook. Use its own helpers so loan
	repayments and ``do_not_include_in_total`` remain correct, then refresh all
	period totals that the standard validation calculated before our rows existed.
	"""
	exchange_rate = flt(doc.exchange_rate) or 1
	doc.gross_pay = doc.get_component_totals("earnings")
	doc.base_gross_pay = flt(flt(doc.gross_pay) * exchange_rate, doc.precision("base_gross_pay"))
	doc.set_net_pay()
	doc.compute_year_to_date()
	doc.compute_month_to_date()
	doc.compute_component_wise_year_to_date()


def _upsert(rows, component_name, amount, doc, table):
	component = frappe.get_cached_doc("Salary Component", component_name)
	values = {
		"salary_component": component_name,
		"abbr": component.salary_component_abbr,
		"amount": amount,
		"default_amount": 0,
		"additional_amount": 0,
		"depends_on_payment_days": component.depends_on_payment_days,
		"do_not_include_in_total": component.do_not_include_in_total,
		"do_not_include_in_accounts": component.do_not_include_in_accounts,
		"accrual_component": component.accrual_component,
		"is_tax_applicable": component.is_tax_applicable,
		"is_flexible_benefit": component.is_flexible_benefit,
		"variable_based_on_taxable_salary": component.variable_based_on_taxable_salary,
		"exempted_from_income_tax": component.exempted_from_income_tax,
		"deduct_full_tax_on_selected_payroll_date": component.deduct_full_tax_on_selected_payroll_date,
	}
	for row in rows:
		if row.salary_component == component_name and not row.additional_salary:
			row.update(values)
			return
	doc.append(table, values)


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

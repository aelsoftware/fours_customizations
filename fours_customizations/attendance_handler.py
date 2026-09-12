"""
attendance_handler.py — Nightly attendance creation (Req #2).

`create_daily_attendance()` runs every night at the time configured in
Four S Industries Settings (default 23:00).  For every active employee it:

  1. Reads all check-in / check-out logs of type "IN" / "OUT" for the day.
  2. If no log exists at all → status "Absent".
  3. Otherwise → status "Present", earliest IN as `in_time`, latest OUT as
     `out_time`, and the late / early-exit / overtime flags computed against
     the configured work window.

Already-existing attendance records are left alone so re-runs are safe.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import frappe
from frappe.utils import (
	get_datetime,
	get_time,
	getdate,
	now_datetime,
	time_diff_in_hours,
)
from hrms.utils.holiday_list import get_holiday_list_for_employee

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_settings,
)


def create_daily_attendance(target_date: str | None = None) -> dict:
	"""Process attendance for `target_date` (default = yesterday → today depending on time).

	Returns a small summary dict suitable for logging.
	"""
	settings = get_settings()
	if not int(settings.enable_attendance_automation or 0):
		return {"skipped": True, "reason": "disabled"}

	# Use today's date — the job is scheduled to run at the end of the day.
	day = getdate(target_date) if target_date else getdate(now_datetime())

	work_start = get_time(settings.work_start_time or "08:00:00")
	work_end = get_time(settings.work_end_time or "17:00:00")
	late_threshold = int(settings.late_threshold_minutes or 15)
	min_overtime = int(settings.minimum_overtime_minutes or 30)

	employees = frappe.get_all(
		"Employee",
		filters={"status": "Active"},
		fields=["name", "employee_name", "company", "default_shift", "holiday_list"],
	)
	for holiday_list in {_resolve_holiday_list(emp, day) for emp in employees} - {None}:
		_ensure_holiday_list_coverage(holiday_list, day)

	created, updated, skipped, absent_count = 0, 0, 0, 0

	for emp in employees:
		try:
			if frappe.db.exists("Attendance", {"employee": emp.name, "attendance_date": day, "docstatus": ("!=", 2)}):
				skipped += 1
				continue

			logs = frappe.get_all(
				"Employee Checkin",
				filters={
					"employee": emp.name,
					"time": ["between", [
						f"{day} 00:00:00",
						f"{day} 23:59:59",
					]],
				},
				fields=["name", "time", "log_type"],
				order_by="time asc",
			)

			ins = [log for log in logs if (log.log_type or "").upper() == "IN" or not log.log_type]
			outs = [log for log in logs if (log.log_type or "").upper() == "OUT"]

			if not logs:
				_create_absent(emp, day)
				absent_count += 1
				continue

			in_time = ins[0].time if ins else logs[0].time
			out_time = outs[-1].time if outs else None

			in_dt = get_datetime(in_time)
			out_dt = get_datetime(out_time) if out_time else None

			late = False
			if in_dt:
				work_start_dt = datetime.combine(day, work_start)
				tolerance = work_start_dt + timedelta(minutes=late_threshold)
				late = in_dt > tolerance

			early_exit = False
			if out_dt:
				work_end_dt = datetime.combine(day, work_end)
				early_exit = out_dt < work_end_dt

			overtime_hours = 0.0
			if out_dt and int(settings.overtime_eligible or 0):
				work_end_dt = datetime.combine(day, work_end)
				if out_dt > work_end_dt:
					diff = time_diff_in_hours(out_dt, work_end_dt)
					if diff * 60 >= min_overtime:
						overtime_hours = round(diff, 2)

			attendance = frappe.new_doc("Attendance")
			attendance.employee = emp.name
			attendance.employee_name = emp.employee_name
			attendance.attendance_date = day
			attendance.company = emp.company
			attendance.status = "Present"
			attendance.in_time = in_dt
			attendance.out_time = out_dt
			attendance.late_entry = 1 if late else 0
			attendance.early_exit = 1 if early_exit else 0
			if overtime_hours:
				attendance.working_hours = float(overtime_hours)
			attendance.flags.ignore_permissions = True
			try:
				attendance.insert()
				attendance.submit()
				created += 1
			except Exception:
				frappe.log_error(frappe.get_traceback(), f"4S Attendance: create failed for {emp.name}")

		except Exception:
			frappe.log_error(frappe.get_traceback(), f"4S Attendance: top-level failure for {emp.name}")

	frappe.db.commit()
	return {
		"date": str(day),
		"created": created,
		"updated": updated,
		"skipped": skipped,
		"absent": absent_count,
	}


def _create_absent(emp, day) -> None:
	"""Create an absent Attendance row for the employee.

	Skips employees who have no submitted Shift Assignment for the day (they are
	not on a clock-in schedule — e.g. salaried staff — so must not be marked
	absent or deducted), and skips holidays. The holiday list is resolved from
	the employee, falling back to the company default (employee-level holiday
	lists are usually blank here)."""
	if not _has_shift_assignment(emp.name, day):
		return
	if _is_holiday(_resolve_holiday_list(emp, day), day):
		return

	attendance = frappe.new_doc("Attendance")
	attendance.employee = emp.name
	attendance.employee_name = emp.employee_name
	attendance.attendance_date = day
	attendance.company = emp.company
	attendance.status = "Absent"
	attendance.flags.ignore_permissions = True
	try:
		attendance.insert()
		attendance.submit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"4S Attendance: absent failed for {emp.name}")


def _is_holiday(holiday_list: str | None, day) -> bool:
	if not holiday_list:
		return False
	day = getdate(day)
	if frappe.db.exists("Holiday", {"parent": holiday_list, "holiday_date": day}):
		return True

	# Weekly offs are a standing work-schedule rule, not a one-year exception.
	# Keep honouring them if somebody forgets to roll the Holiday List's dated
	# rows forward; otherwise the nightly job silently marks every Sunday absent.
	weekly_off = frappe.db.get_value("Holiday List", holiday_list, "weekly_off", cache=True)
	return bool(weekly_off and day.strftime("%A") == weekly_off)


def _ensure_holiday_list_coverage(holiday_list: str, day) -> None:
	"""Roll a country Holiday List through ``day`` when its range has expired.

	This keeps both weekly offs and the selected country's public holidays
	current without requiring a manual calendar rollover every January.
	"""
	day = getdate(day)
	doc = frappe.get_doc("Holiday List", holiday_list)
	if doc.to_date and day <= getdate(doc.to_date):
		return

	doc.to_date = day.replace(month=12, day=31)
	if doc.weekly_off:
		doc.get_weekly_off_dates()
	if doc.country:
		doc.get_local_holidays()
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	frappe.clear_cache(doctype="Holiday List")


def _resolve_holiday_list(emp, day=None) -> str | None:
	"""Resolve the effective HRMS Holiday List, with legacy-field fallback."""
	holiday_list = get_holiday_list_for_employee(
		emp.name,
		raise_exception=False,
		as_on=day,
	)
	if holiday_list:
		return holiday_list
	return emp.get("holiday_list") or (
		frappe.db.get_value("Company", emp.get("company"), "default_holiday_list")
		if emp.get("company")
		else None
	)


def _has_shift_assignment(employee: str, day) -> bool:
	"""True if the employee has a submitted Shift Assignment covering `day`
	(a blank end_date is treated as ongoing only while the assignment is Active).

	HRMS automatically marks date-bounded assignments Inactive after they expire,
	so those rows remain valid historical evidence.  An Inactive open-ended row,
	on the other hand, has been explicitly switched off and must not keep creating
	absences forever.
	"""
	return bool(
		frappe.db.sql(
			"""
			SELECT 1 FROM `tabShift Assignment`
			WHERE employee = %(emp)s AND docstatus = 1
			  AND start_date <= %(day)s
			  AND (end_date IS NULL OR end_date >= %(day)s)
			  AND (status = 'Active' OR end_date IS NOT NULL)
			LIMIT 1
			""",
			{"emp": employee, "day": day},
		)
	)


def hourly_attendance_dispatcher():
	"""Hourly cron entry point — runs `create_daily_attendance` only at the
	configured attendance creation hour.  Frappe's scheduler granularity is
	hourly without ad-hoc Crontab manipulation, so we gate by time here.
	"""
	settings = get_settings()
	if not int(settings.enable_attendance_automation or 0):
		return
	target = get_time(settings.attendance_creation_time or "23:00:00")
	now = now_datetime().time()
	# Run if we are in the same hour as the configured time (10-min slack window)
	if now.hour != target.hour:
		return
	create_daily_attendance()

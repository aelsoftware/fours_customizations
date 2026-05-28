"""
attendance_handler.py — Nightly attendance creation (Req #2).

`create_daily_attendance()` runs every night at the time configured in
Fours Industries Settings (default 23:00).  For every active employee it:

  1. Reads all check-in / check-out logs of type "IN" / "OUT" for the day.
  2. If no log exists at all → status "Absent".
  3. Otherwise → status "Present", earliest IN as `in_time`, latest OUT as
     `out_time`, and the late / early-exit / overtime flags computed against
     the configured work window.

Already-existing attendance records are left alone so re-runs are safe.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta

import frappe
from frappe.utils import (
	add_to_date,
	get_datetime,
	get_time,
	getdate,
	now_datetime,
	time_diff_in_hours,
)

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
	"""Create an absent Attendance row for the employee, ignoring weekends/holidays."""
	if _is_holiday(emp.holiday_list, day):
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
	return bool(
		frappe.db.exists("Holiday", {"parent": holiday_list, "holiday_date": day})
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

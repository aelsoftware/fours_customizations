"""
checkin_handler.py — Device checkin ingestion + per-punch attendance upkeep.

Replaces the "Employee Checkin" Server Script on the site (disable that script
after deploying this — otherwise both will fight over the same attendance).

Rules:

  • A device punch is NEVER rejected for business reasons — every log the
    device produced is stored as an Employee Checkin.  Re-sent logs are
    answered idempotently with the existing record instead of an error.
  • The EARLIEST IN of the day drives the attendance `in_time` and the
    late-entry / early-entry flags. It is applied the moment each punch is
    received, so attendance is always current.
  • The LAST OUT of the day drives `out_time`, the early-exit flag and the
    overtime fields — a later checkout always recomputes them.
  • A punch before 10:00 (even one the device labelled OUT) is treated as an
    arrival, not a departure — it seeds/refreshes the earliest in_time.
  • A checkout with no check-in at all synthesizes a 09:00 arrival (and a
    traceable SYSTEM-AUTO check-in) instead of erroring.
  • Attendance failures never block the checkin itself; they are logged to
    the Error Log instead.

API endpoint (accepts the same payload shape as
hrms…employee_checkin.add_log_based_on_employee_field):

    POST /api/method/fours_customizations.checkin_handler.add_checkin
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, get_time, getdate

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_settings,
)

EARLY_ENTRY_WINDOW_MINUTES = 50  # window before shift start that counts as "early entry"
LATE_GRACE_MINUTES = 10  # grace after shift start before an IN counts as late

# A punch (even one the device labelled OUT) before this time is treated as an
# arrival, not a departure — nobody leaves for the day this early.
CHECKOUT_AS_CHECKIN_BEFORE = dt_time(10, 0)
# When a checkout arrives with no check-in at all, synthesize this arrival time
# instead of leaving the day half-open (the old server script errored here).
MISSING_CHECKIN_FALLBACK = dt_time(9, 0)


# ── API ─────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def add_checkin(
	employee_field_value=None,
	timestamp=None,
	device_id=None,
	log_type=None,
	employee_fieldname="employee",
	**kwargs,
):
	"""Idempotent replacement for hrms `add_log_based_on_employee_field`.

	Returns {"status": "created" | "duplicate" | "unknown_employee", ...} with
	HTTP 200 for all business outcomes, so the integration can mark the log as
	delivered.  Only genuine server/validation faults raise (→ retried later).
	"""
	if not employee_field_value or not timestamp:
		frappe.throw(_("'employee_field_value' and 'timestamp' are required."))

	log_type = (log_type or "").strip().upper()
	if log_type not in ("IN", "OUT"):
		frappe.throw(_("'log_type' must be IN or OUT."))

	employee = _resolve_employee(employee_field_value, employee_fieldname)
	if not employee:
		# Device user with no Employee record — acknowledge so the
		# integration does not retry forever, but report it clearly.
		return {"status": "unknown_employee", "employee_field_value": employee_field_value}

	time = get_datetime(timestamp)

	# Idempotency: same employee + same instant ⇒ same punch (mirrors the
	# duplicate-log validation in hrms, which would otherwise throw).
	existing = frappe.db.get_value(
		"Employee Checkin",
		{"employee": employee, "time": time},
		["name", "attendance"],
		as_dict=True,
	)
	if existing:
		return {
			"status": "duplicate",
			"checkin": existing.name,
			"attendance": existing.attendance,
			"employee": employee,
		}

	checkin = frappe.new_doc("Employee Checkin")
	checkin.employee = employee
	checkin.time = time
	checkin.log_type = log_type
	checkin.device_id = device_id
	checkin.flags.ignore_permissions = True
	checkin.insert()  # update_attendance_from_checkin runs on after_insert

	return {
		"status": "created",
		"checkin": checkin.name,
		"attendance": checkin.attendance,
		"employee": employee,
	}


def _resolve_employee(value, fieldname=None):
	"""Resolve an Employee name from the value the device/integration sent."""
	if frappe.db.exists("Employee", value):
		return value

	meta = frappe.get_meta("Employee")
	for field in dict.fromkeys([fieldname, "attendance_device_id", "employee_number"]):
		if field and field != "name" and meta.has_field(field):
			name = frappe.db.get_value("Employee", {field: value})
			if name:
				return name
	return None


# ── doc_events: Employee Checkin.after_insert ──────────────────────────────

def update_attendance_from_checkin(doc, method=None):
	"""Keep the day's Attendance in sync with this punch — synchronously, so the
	attendance reflects the punch the moment it is received.

	Never raises — a failure here must not reject the punch itself.
	"""
	# Punches we synthesize ourselves already updated the attendance in the same
	# pass; skip to avoid re-processing them.
	if doc.flags.get("from_auto_checkin"):
		return
	try:
		_process_checkin(doc)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"4S Checkin: attendance update failed for {doc.name}",
		)


def _process_checkin(checkin):
	if not checkin.employee or not checkin.time:
		return
	log_type = (checkin.log_type or "").strip().upper()
	if log_type not in ("IN", "OUT"):
		return

	checkin_time = get_datetime(checkin.time)
	day = getdate(checkin_time)
	shift = checkin.shift or frappe.db.get_value("Employee", checkin.employee, "default_shift")

	# An early-morning checkout is really an arrival — treat any OUT before
	# 10:00 as a check-in so it can seed the (earliest) in_time.
	if log_type == "OUT" and checkin_time.time() < CHECKOUT_AS_CHECKIN_BEFORE:
		log_type = "IN"

	attendance = _find_attendance(checkin.employee, day, shift)
	if not attendance:
		attendance = _create_attendance(checkin.employee, day, shift)

	if log_type == "IN":
		_apply_in(attendance, checkin_time, day, shift)
	else:
		_apply_out(attendance, checkin_time, day, shift)

	if checkin.attendance != attendance.name:
		checkin.db_set("attendance", attendance.name, update_modified=False)


def _find_attendance(employee, day, shift):
	filters = {"employee": employee, "attendance_date": day, "docstatus": ["<", 2]}
	name = None
	if shift:
		name = frappe.db.get_value("Attendance", dict(filters, shift=shift))
	if not name:
		# also match attendance created without a shift (e.g. the nightly job)
		name = frappe.db.get_value("Attendance", filters)
	return frappe.get_doc("Attendance", name) if name else None


def _create_attendance(employee, day, shift):
	attendance = frappe.new_doc("Attendance")
	attendance.employee = employee
	attendance.attendance_date = day
	attendance.status = "Present"
	attendance.shift = shift
	attendance.company = frappe.db.get_value("Employee", employee, "company")
	attendance.flags.ignore_permissions = True
	attendance.insert()
	attendance.submit()
	return attendance


def _apply_in(attendance, checkin_time, day, shift):
	updates = {}
	if attendance.status == "Absent":
		updates["status"] = "Present"

	current_in = get_datetime(attendance.in_time) if attendance.in_time else None
	if current_in is None or checkin_time < current_in:
		# earliest checkin of the day wins — recompute the entry flags off it
		updates["in_time"] = checkin_time
		_set_entry_flags(updates, checkin_time, day, shift)

	_save_attendance(attendance, updates)


def _apply_out(attendance, checkin_time, day, shift):
	current_out = get_datetime(attendance.out_time) if attendance.out_time else None
	if current_out and checkin_time <= current_out:
		return  # an earlier OUT — the latest checkout stays final

	updates = {"out_time": checkin_time}
	if attendance.status == "Absent":
		updates["status"] = "Present"

	# Checkout with no check-in at all (e.g. an evening punch and nothing in the
	# morning): synthesize a 09:00 arrival instead of leaving the day half-open.
	if not attendance.in_time:
		synth_in = datetime.combine(day, MISSING_CHECKIN_FALLBACK)
		updates["in_time"] = synth_in
		_set_entry_flags(updates, synth_in, day, shift)
		_create_auto_checkin(attendance.employee, synth_in, shift, attendance.name)

	_start, shift_end = _shift_window(day, shift)
	if shift_end:
		updates["early_exit"] = 1 if checkin_time < shift_end else 0

	updates.update(_overtime_updates(attendance, checkin_time, day))
	_save_attendance(attendance, updates)


def _set_entry_flags(updates, in_dt, day, shift):
	"""Compute late_entry / custom_early_entry for an arrival at `in_dt`."""
	shift_start, _end = _shift_window(day, shift)
	if not shift_start:
		return
	late_cutoff = shift_start + timedelta(minutes=LATE_GRACE_MINUTES)
	updates["late_entry"] = 1 if in_dt > late_cutoff else 0
	early_window = shift_start - timedelta(minutes=EARLY_ENTRY_WINDOW_MINUTES)
	updates["custom_early_entry"] = 1 if early_window <= in_dt < shift_start else 0


def _create_auto_checkin(employee, time, shift, attendance_name):
	"""Insert a traceable IN punch for a synthesized arrival.

	Flagged `from_auto_checkin` so the after_insert hook skips it — the caller
	is already updating the attendance in the same pass.
	"""
	if frappe.db.exists("Employee Checkin", {"employee": employee, "time": time}):
		return
	checkin = frappe.new_doc("Employee Checkin")
	checkin.employee = employee
	checkin.time = time
	checkin.log_type = "IN"
	checkin.shift = shift
	checkin.device_id = "SYSTEM-AUTO"
	checkin.attendance = attendance_name
	checkin.flags.from_auto_checkin = True
	checkin.flags.ignore_permissions = True
	checkin.insert()


def _overtime_updates(attendance, checkin_time, day):
	"""Recompute the overtime custom fields off this (latest) checkout."""
	if not attendance.meta.has_field("custom_overtime_duration"):
		return {}
	if getattr(attendance, "custom_no_checkout", 0):
		return {}

	ot_start_value = getattr(attendance, "custom_overtime_start_", None)
	if not ot_start_value:
		return {}

	ot_start = datetime.combine(day, _to_time(ot_start_value))
	ot_limit = None
	if getattr(attendance, "custom_overtime_limit", None):
		ot_limit = datetime.combine(day, _to_time(attendance.custom_overtime_limit))
		if ot_limit <= ot_start:
			ot_limit += timedelta(days=1)

	if checkin_time < ot_start:
		return {"custom_overtime": 0, "custom_overtime_duration": 0}

	effective = min(checkin_time, ot_limit) if ot_limit else checkin_time
	minutes = max(0, round((effective - ot_start).total_seconds() / 60, 2))
	return {
		"custom_overtime": 1 if minutes > 0 else 0,
		"custom_overtime_duration": minutes,
	}


# ── helpers ─────────────────────────────────────────────────────────────────

def _save_attendance(attendance, updates):
	updates = {k: v for k, v in updates.items() if attendance.meta.has_field(k)}
	if not updates:
		return
	if attendance.docstatus == 0:
		attendance.update(updates)
		attendance.flags.ignore_permissions = True
		attendance.save()
	else:
		attendance.db_set(updates, notify=True)


def _shift_window(day, shift):
	"""(start_dt, end_dt) of the working window on `day` — from the Shift Type,
	falling back to the configured 4S work window.  Either may be None."""
	start = end = None
	if shift:
		row = frappe.db.get_value("Shift Type", shift, ["start_time", "end_time"], as_dict=True)
		if row:
			start, end = row.start_time, row.end_time

	if not start and not end:
		try:
			settings = get_settings()
			start = settings.work_start_time
			end = settings.work_end_time
		except Exception:
			pass

	start_dt = datetime.combine(day, _to_time(start)) if start else None
	end_dt = datetime.combine(day, _to_time(end)) if end else None
	if start_dt and end_dt and end_dt <= start_dt:
		end_dt += timedelta(days=1)  # overnight shift
	return start_dt, end_dt


def _to_time(value):
	if isinstance(value, timedelta):
		return (datetime.min + value).time()
	return get_time(value)

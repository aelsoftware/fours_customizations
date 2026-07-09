"""
Employee Handler — Fours Customizations
=======================================

Drives an employee's **Shift Assignment** and **Salary Structure Assignment**
directly off the Employee record, so both stay at exactly one active assignment
per employee (replacing the old weekly server script that created one Shift
Assignment per day).

on_update (fires on insert and on every save)
  Shift Assignment — keyed off ``default_shift``:
    • New employee (or the shift changed): cancel any current/future submitted
      Shift Assignment and create ONE open-ended assignment (no end date) for
      the new shift. Past assignments are left untouched.
    • No ``default_shift`` set: nothing is created — the employee is not on a
      clock-in schedule and is excluded from attendance deductions.

  Salary Structure Assignment — keyed off ``ctc`` (Cost to Company):
    • CTC set and no assignment yet: create one carrying ``base = ctc`` (using
      the company's salary structure — resolved from the ``default_salary_structure``
      setting, or the company's single active structure).
    • CTC changed with an existing assignment: a draft is edited in place; a
      submitted one is cancelled and re-created as an amendment with the new base.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate, nowdate

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_setting,
)


def on_update(doc, method=None):
	"""Sync the employee's shift + salary-structure assignments. Fires on insert
	(previous is None) and on every subsequent save."""
	if getattr(doc, "_4s_emp_syncing", False):
		return

	previous = doc.get_doc_before_save()

	doc._4s_emp_syncing = True
	try:
		try:
			_sync_shift_assignment(doc, previous)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"4S Employee: shift assignment sync failed for {doc.name}")
			frappe.msgprint(
				"Could not automatically update the Shift Assignment — please set it "
				"manually. Details are in the Error Log.",
				indicator="orange", alert=True,
			)
		try:
			_sync_salary_structure_assignment(doc, previous)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"4S Employee: salary structure sync failed for {doc.name}")
			frappe.msgprint(
				"Could not automatically update the Salary Structure Assignment — please "
				"set it manually. Details are in the Error Log.",
				indicator="orange", alert=True,
			)
	finally:
		doc._4s_emp_syncing = False


# ── Shift Assignment ─────────────────────────────────────────────────────────

def _sync_shift_assignment(doc, previous):
	shift = doc.default_shift
	if not shift:
		return  # not on a shift schedule → nothing to assign

	# Only act when the shift is newly set or changed.
	if previous is not None and previous.default_shift == doc.default_shift:
		return

	today = getdate(nowdate())
	doj = getdate(doc.date_of_joining) if doc.date_of_joining else None
	from_date = doj if (doj and doj > today) else today

	# Cancel any current or future submitted assignments so the new one doesn't
	# overlap and the employee ends up with a single active assignment. Past
	# assignments (already ended) are left for the historical record.
	existing = frappe.get_all(
		"Shift Assignment",
		filters={"employee": doc.name, "docstatus": 1},
		fields=["name", "end_date"],
	)
	for row in existing:
		if row.end_date is None or getdate(row.end_date) >= from_date:
			sa = frappe.get_doc("Shift Assignment", row.name)
			sa.flags.ignore_permissions = True
			sa.cancel()

	sa = frappe.new_doc("Shift Assignment")
	sa.employee = doc.name
	sa.shift_type = shift
	sa.company = doc.company
	sa.status = "Active"
	sa.start_date = from_date
	# end_date intentionally left blank → one ongoing assignment.
	sa.flags.ignore_permissions = True
	sa.insert(ignore_permissions=True)
	sa.submit()
	frappe.msgprint(f"Shift Assignment {sa.name} created for shift '{shift}'.", alert=True)


# ── Salary Structure Assignment ──────────────────────────────────────────────

def _sync_salary_structure_assignment(doc, previous):
	new_base = flt(doc.ctc)
	if new_base <= 0:
		return

	# Only act when CTC is newly set or changed.
	if previous is not None and flt(previous.ctc) == new_base:
		return

	assignment = _latest_assignment(doc.name)
	if not assignment:
		_create_salary_structure_assignment(doc, new_base)
		return

	if assignment.docstatus == 0:
		_update_draft_assignment(assignment.name, new_base)
	elif assignment.docstatus == 1:
		_amend_submitted_assignment(assignment.name, new_base)


def _resolve_salary_structure(company: str) -> str | None:
	"""Salary Structure to assign: the configured default (if it belongs to the
	company), else the company's single active structure, else None (ambiguous)."""
	default = get_setting("default_salary_structure")
	if default and frappe.db.get_value("Salary Structure", default, "company") == company:
		return default
	structs = frappe.get_all(
		"Salary Structure",
		filters={"company": company, "is_active": "Yes", "docstatus": 1},
		pluck="name",
	)
	return structs[0] if len(structs) == 1 else None


def _create_salary_structure_assignment(doc, base: float):
	structure = _resolve_salary_structure(doc.company)
	if not structure:
		frappe.msgprint(
			"Cost to Company was set but no Salary Structure Assignment could be "
			"created automatically — the company has several active salary structures. "
			"Set 'Default Salary Structure' in Four S Industries Settings, or create the "
			"assignment manually.",
			indicator="orange", alert=True,
		)
		return

	today = getdate(nowdate())
	doj = getdate(doc.date_of_joining) if doc.date_of_joining else None
	from_date = doj if doj else today

	ssa = frappe.new_doc("Salary Structure Assignment")
	ssa.employee = doc.name
	ssa.salary_structure = structure
	ssa.company = doc.company
	ssa.from_date = from_date
	ssa.base = base
	ssa.flags.ignore_permissions = True
	ssa.insert(ignore_permissions=True)
	ssa.submit()
	frappe.msgprint(
		f"Salary Structure Assignment {ssa.name} created with base {base:,.2f}.",
		alert=True,
	)


def _latest_assignment(employee: str):
	"""Most recent active (draft or submitted) Salary Structure Assignment."""
	return frappe.db.get_value(
		"Salary Structure Assignment",
		{"employee": employee, "docstatus": ["<", 2]},
		["name", "docstatus", "base", "from_date"],
		order_by="docstatus desc, from_date desc",
		as_dict=True,
	)


def _update_draft_assignment(name: str, new_base: float):
	if flt(frappe.db.get_value("Salary Structure Assignment", name, "base")) == new_base:
		return
	ssa = frappe.get_doc("Salary Structure Assignment", name)
	ssa.base = new_base
	ssa.flags.ignore_permissions = True
	ssa.save(ignore_permissions=True)
	frappe.msgprint(
		f"Salary Structure Assignment {ssa.name} updated with the new base {new_base:,.2f}.",
		alert=True,
	)


def _amend_submitted_assignment(name: str, new_base: float):
	old = frappe.get_doc("Salary Structure Assignment", name)
	if flt(old.base) == new_base:
		return

	# Cancel the immutable submitted assignment, then re-create it as an
	# amendment carrying the new base — mirroring ERPNext's amend-from flow.
	old.flags.ignore_permissions = True
	old.cancel()

	amended = frappe.copy_doc(old)
	amended.amended_from = old.name
	amended.base = new_base
	amended.docstatus = 0
	amended.flags.ignore_permissions = True
	amended.insert(ignore_permissions=True)
	amended.submit()

	frappe.msgprint(
		f"Salary Structure Assignment {old.name} cancelled and amended as "
		f"{amended.name} with the new base {new_base:,.2f}.",
		alert=True,
	)

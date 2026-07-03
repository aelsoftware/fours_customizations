"""
Employee Salary Deduction Handler — Fours Customizations
=========================================================

Keeps Employee Salary Deduction records and Salary Slips in lock-step:

  validate / before_cancel
    If a *submitted* Salary Slip already covers the deduction's date for that
    employee, block the operation with an explicit error — the slip must be
    cancelled first.

  on_submit / on_cancel
    If a *draft* Salary Slip covers the period, re-save it so the deduction
    totals are recalculated (see salary_slip_handler.calculate_and_add_deductions,
    which pulls submitted Employee Salary Deductions into the slip).
"""

from __future__ import annotations

import frappe
from frappe.utils import getdate


def _find_salary_slip(doc):
	"""Return the active (draft or submitted) Salary Slip whose period covers
	this deduction's date, or None."""
	if not doc.employee or not doc.date:
		return None
	deduction_date = getdate(doc.date)
	return frappe.db.get_value(
		"Salary Slip",
		{
			"employee": doc.employee,
			"docstatus": ["<", 2],
			"start_date": ["<=", deduction_date],
			"end_date": [">=", deduction_date],
		},
		["name", "docstatus", "start_date", "end_date"],
		as_dict=True,
	)


def _block_if_slip_submitted(doc):
	slip = _find_salary_slip(doc)
	if slip and slip.docstatus == 1:
		frappe.throw(
			f"Salary Slip <b>{slip.name}</b> was already submitted for that period "
			f"({slip.start_date} to {slip.end_date}). "
			"Please cancel the salary slip first, then save this deduction again.",
			title="Salary Slip Already Submitted",
		)


def validate(doc, method=None):
	"""Runs on save and on submit."""
	_block_if_slip_submitted(doc)


def before_cancel(doc, method=None):
	"""A deduction inside a submitted slip can't silently disappear — the slip
	must be cancelled first, exactly like on creation."""
	_block_if_slip_submitted(doc)


def on_submit(doc, method=None):
	_sync_salary_slip(doc)


def on_cancel(doc, method=None):
	_sync_salary_slip(doc)


def _sync_salary_slip(doc):
	"""Re-save the draft Salary Slip covering this deduction's period so its
	deduction rows are recalculated from the current set of submitted
	Employee Salary Deductions."""
	slip = _find_salary_slip(doc)
	if not slip or slip.docstatus != 0:
		return

	try:
		slip_doc = frappe.get_doc("Salary Slip", slip.name)
		slip_doc.flags.ignore_permissions = True
		slip_doc.save(ignore_permissions=True)
		frappe.msgprint(
			f"Salary Slip {slip.name} updated with the salary deduction changes.",
			alert=True,
		)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"4S ESD: failed to update Salary Slip {slip.name}",
		)
		frappe.msgprint(
			f"Could not auto-update Salary Slip {slip.name} — please open and "
			"save it manually. Details are in the Error Log.",
			indicator="orange",
			alert=True,
		)

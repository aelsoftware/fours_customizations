"""
Employee Handler — Fours Customizations
=======================================

Keeps the employee's Salary Structure Assignment in step with the salary
figure held on the Employee record.

on_update
  When an Employee's **Cost to Company (CTC)** changes, the latest Salary
  Structure Assignment is updated to carry the new amount as its ``base``:

    • A *draft* assignment is edited in place.
    • A *submitted* assignment is cancelled and re-created as an amendment
      (same from_date / salary structure) carrying the new base, then
      re-submitted — the only way to change a submitted, immutable document.

  If no assignment exists yet, there is nothing to update.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt


def on_update(doc, method=None):
	"""Propagate a changed CTC to the employee's latest Salary Structure Assignment."""
	# Guard against re-entrancy (cancel/submit below re-saves nothing on Employee,
	# but stay defensive against nested saves).
	if getattr(doc, "_4s_ctc_syncing", False):
		return

	previous = doc.get_doc_before_save()
	if previous is None:
		return  # brand-new employee — no assignment to sync yet

	if flt(previous.ctc) == flt(doc.ctc):
		return  # CTC unchanged

	new_base = flt(doc.ctc)
	if new_base <= 0:
		return

	assignment = _latest_assignment(doc.name)
	if not assignment:
		return

	doc._4s_ctc_syncing = True
	try:
		if assignment.docstatus == 0:
			_update_draft_assignment(assignment.name, new_base)
		elif assignment.docstatus == 1:
			_amend_submitted_assignment(assignment.name, new_base)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"4S Employee: failed to sync Salary Structure Assignment for {doc.name}",
		)
		frappe.msgprint(
			"Could not automatically update the Salary Structure Assignment with the "
			"new Cost to Company — please update it manually. Details are in the Error Log.",
			indicator="orange",
			alert=True,
		)
	finally:
		doc._4s_ctc_syncing = False


def _latest_assignment(employee: str):
	"""Return the most recent active (draft or submitted) Salary Structure
	Assignment for the employee, preferring the latest from_date."""
	row = frappe.db.get_value(
		"Salary Structure Assignment",
		{"employee": employee, "docstatus": ["<", 2]},
		["name", "docstatus", "base", "from_date"],
		order_by="docstatus desc, from_date desc",
		as_dict=True,
	)
	return row


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

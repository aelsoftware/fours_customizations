"""Vox-owned Kit POS configuration.

The shared ``kit_pos`` app supplies the shift, void-audit, and report
features.  The Vox companies, role names, default shifts, and role grants
belong here so installing Kit POS for another customer never creates Vox
configuration.
"""

from __future__ import annotations

import frappe

VOX_ACCOUNTANT_ROLE = "Vox Accountant"
BAR_SHIFT_REPORT = "Bar Shift Sales Report"
VOX_SHIFTS = (
	("Vox Lounge Nansana Bar Shift", "Vox Lounge Nansana"),
	("Vox Lounge Makindye Bar Shift", "Vox Lounge"),
)

DOCTYPE_RIGHTS = {
	"Sales Invoice": frozenset({"read", "report"}),
	"Kit POS Shift": frozenset({"read", "report", "export", "print"}),
	"Voided Bill": frozenset(
		{
			"read",
			"report",
			"export",
			"print",
			"email",
			"create",
			"write",
			"submit",
			"delete",
		}
	),
}


def _vox_workspace_present() -> bool:
	return bool(
		frappe.db.exists("Role", VOX_ACCOUNTANT_ROLE)
		and any(frappe.db.exists("Company", company) for _shift, company in VOX_SHIFTS)
	)


def _grant_doctype_rights(doctype: str, rights: frozenset[str]) -> bool:
	"""Add the former standard role grant as an additive Custom DocPerm."""
	if not frappe.db.exists("DocType", doctype):
		return False

	from frappe.permissions import add_permission

	filters = {
		"parent": doctype,
		"role": VOX_ACCOUNTANT_ROLE,
		"permlevel": 0,
		"if_owner": 0,
	}
	names = frappe.get_all("Custom DocPerm", filters=filters, pluck="name")
	created = not names
	if created:
		add_permission(
			doctype,
			VOX_ACCOUNTANT_ROLE,
			permlevel=0,
			ptype="read" if "read" in rights else sorted(rights)[0],
		)
		names = frappe.get_all("Custom DocPerm", filters=filters, pluck="name")
	if not names:
		frappe.throw(f"Could not grant {VOX_ACCOUNTANT_ROLE} access to {doctype}.")

	values = {right: 1 for right in rights}
	for name in names:
		frappe.db.set_value("Custom DocPerm", name, values, update_modified=False)
	frappe.clear_cache(doctype=doctype)
	return created


def _ensure_report_role() -> bool:
	if not frappe.db.exists("Report", BAR_SHIFT_REPORT):
		return False
	filters = {
		"parent": BAR_SHIFT_REPORT,
		"parenttype": "Report",
		"parentfield": "roles",
		"role": VOX_ACCOUNTANT_ROLE,
	}
	if frappe.db.exists("Has Role", filters):
		return False
	frappe.get_doc({"doctype": "Has Role", **filters}).db_insert()
	frappe.clear_cache(doctype="Report")
	return True


def ensure_vox_kit_pos_permissions() -> dict:
	"""Restore Vox access after the universal app syncs generic permissions."""
	if not _vox_workspace_present():
		return {"status": "not_applicable", "permissions_created": [], "report_role_created": False}

	# Generic audit/location access remains implemented by Kit POS, while this
	# customer app explicitly opts its own accountant role into that facility.
	from kit_pos.setup.voided_bill import setup_permissions

	setup_permissions(roles=(VOX_ACCOUNTANT_ROLE,))
	created = [
		doctype
		for doctype, rights in DOCTYPE_RIGHTS.items()
		if _grant_doctype_rights(doctype, rights)
	]
	report_created = _ensure_report_role()
	return {
		"status": "configured",
		"permissions_created": created,
		"report_role_created": report_created,
	}


def ensure_vox_bar_shifts() -> dict:
	"""Create or converge each Vox company's recurring overnight POS shift."""
	if not frappe.db.exists("DocType", "Kit POS Shift"):
		return {"status": "not_applicable", "shifts": []}

	configured = []
	for shift_name, company in VOX_SHIFTS:
		if not frappe.db.exists("Company", company):
			continue

		frappe.db.set_value(
			"Kit POS Shift",
			{
				"company": company,
				"is_default": 1,
				"name": ["!=", shift_name],
			},
			"is_default",
			0,
			update_modified=False,
		)

		if frappe.db.exists("Kit POS Shift", shift_name):
			shift = frappe.get_doc("Kit POS Shift", shift_name)
		else:
			shift = frappe.new_doc("Kit POS Shift")
			shift.shift_name = shift_name

		shift.company = company
		shift.start_time = "10:00:00"
		shift.end_time = "09:59:59"
		shift.is_default = 1
		shift.disabled = 0
		shift.save(ignore_permissions=True)
		configured.append(shift_name)

	frappe.clear_cache(doctype="Kit POS Shift")
	return {"status": "configured" if configured else "not_applicable", "shifts": configured}


def ensure_vox_kit_pos_configuration() -> dict:
	return {
		"permissions": ensure_vox_kit_pos_permissions(),
		"shifts": ensure_vox_bar_shifts(),
	}

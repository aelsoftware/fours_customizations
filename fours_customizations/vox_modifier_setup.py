"""Explicit schema setup only; never import/run it as a business-transaction hook."""

import frappe


def install_schema():
	if frappe.session.user != "Administrator":
		frappe.throw("Only Administrator may install Vox modifier audit storage.", frappe.PermissionError)
	if getattr(frappe.db, "transaction_writes", 0):
		frappe.throw("Install modifier schema in a fresh session, before changing business documents.")
	from kit_pos.connector.modifiers import INVOICE_FIELD

	field = frappe.get_meta("Sales Invoice Item").get_field(INVOICE_FIELD)
	if field is not None:
		_verify_field(field)
	for doctype in ("vox_kitchen_modifier_option", "vox_kitchen_modifier_selection"):
		frappe.reload_doc("fours_customizations", "doctype", doctype, force=True)
	if field is None:
		from kit_pos.setup.modifiers import ensure_schema

		ensure_schema()
		field = frappe.get_meta("Sales Invoice Item").get_field(INVOICE_FIELD)
	_verify_field(field)
	return {
		"audit_doctypes": 2,
		"invoice_field_verified": INVOICE_FIELD,
		"options_published": 0,
		"stock_transactions_created": 0,
	}


def _verify_field(field):
	if (
		not field
		or field.fieldtype != "Data"
		or not all(field.get(flag) for flag in ("hidden", "read_only", "no_copy"))
	):
		frappe.throw("The shared POS modifier reference is incompatible; review it without overwriting it.")
	if field.get("length") and int(field.length) < 140:
		frappe.throw("The shared POS modifier reference cannot hold the immutable audit identity.")

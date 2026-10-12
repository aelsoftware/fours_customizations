"""Keep the BOM dashboard's subcontract links aligned with installed metadata.

Some ERPNext versions still advertise Purchase Receipt/Invoice ``bom`` links
after moving those relationships into Subcontracting Order/Receipt items.
Correct only those obsolete dashboard links; never add columns, rewrite
transactions, or suppress errors from the shared document-count endpoint.
"""

from __future__ import annotations

from copy import deepcopy

import frappe

LEGACY_TARGETS = ("Purchase Receipt", "Purchase Invoice")
SUBCONTRACT_TARGETS = ("Subcontracting Order", "Subcontracting Receipt")


def _has_field(doctype, fieldname, *, require_bom_link=False):
	"""Match the native counter's parent/child-field resolution."""
	if not fieldname:
		return False
	try:
		meta = frappe.get_meta(doctype)
	except frappe.DoesNotExistError:
		return False

	def matches(field):
		return bool(
			field and (not require_bom_link or (field.fieldtype == "Link" and field.options == "BOM"))
		)

	if field := meta.get_field(fieldname):
		return matches(field)
	for table in meta.get_table_fields(include_computed=True):
		try:
			child_meta = frappe.get_meta(table.options)
		except frappe.DoesNotExistError:
			continue
		if field := child_meta.get_field(fieldname):
			return matches(field)
	return False


def get_dashboard_data(data):
	"""Repair obsolete native links without replacing another app's dashboard."""
	result = deepcopy(data)
	# A custom count method may implement indirect relationships that do not
	# have a native parent/child Link. Its contract belongs to that customization.
	if result.get("method") not in (None, "", "frappe.desk.notifications.get_open_count"):
		return result

	groups = result.get("transactions") or []
	present = {target for group in groups for target in group.get("items") or []}
	fieldnames = result.get("non_standard_fieldnames") or {}
	removed = set()
	for target in LEGACY_TARGETS:
		if target not in present:
			continue
		if any(
			(result.get(key) or {}).get(target) for key in ("internal_links", "internal_and_external_links")
		):
			continue
		fieldname = fieldnames.get(target, result.get("fieldname"))
		if not _has_field(target, fieldname):
			removed.add(target)
	if not removed:
		return result

	anchor = None
	for group in groups:
		items = group.get("items") or []
		if removed.intersection(items):
			if anchor is None:
				anchor = group
			group["items"] = [target for target in items if target not in removed]
		for target in removed:
			(group.get("fieldnames") or {}).pop(target, None)
	for target in removed:
		fieldnames.pop(target, None)
	result["non_standard_fieldnames"] = fieldnames

	present -= removed
	disabled = list(result.get("disable_create_buttons") or [])
	for target in SUBCONTRACT_TARGETS:
		if target in present or not _has_field(target, "bom", require_bom_link=True):
			continue
		anchor["items"].append(target)
		# Counts read this map; list clicks can instead read a group's fieldnames.
		# Set both so the badge and its destination use the same child relation.
		fieldnames[target] = "bom"
		if not anchor.get("fieldnames"):
			anchor["fieldnames"] = {}
		anchor["fieldnames"][target] = "bom"
		if target not in disabled:
			disabled.append(target)
		present.add(target)
	result["disable_create_buttons"] = disabled
	return result

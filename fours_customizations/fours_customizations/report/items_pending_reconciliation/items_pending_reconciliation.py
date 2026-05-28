"""Items Pending Reconciliation report — rows for the daily summary email."""

import frappe
from frappe import _


def execute(filters=None):
	columns = [
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 180},
		{"label": _("Item Name"), "fieldname": "item_name", "fieldtype": "Data", "width": 220},
		{"label": _("Warehouse"), "fieldname": "warehouse", "fieldtype": "Link", "options": "Warehouse", "width": 180},
		{"label": _("Qty"), "fieldname": "actual_qty", "fieldtype": "Float", "width": 100},
		{"label": _("Auto-Flagged"), "fieldname": "auto_flagged", "fieldtype": "Check", "width": 100},
		{"label": _("Last Requested"), "fieldname": "last_requested", "fieldtype": "Datetime", "width": 160},
	]

	# Bin-level negative stock
	rows = frappe.db.sql(
		"""
		SELECT
			b.item_code,
			i.item_name,
			b.warehouse,
			b.actual_qty,
			i.custom_negative_stock_auto_enabled AS auto_flagged,
			i.custom_last_reconciliation_request AS last_requested
		FROM `tabBin` b
		INNER JOIN `tabItem` i ON i.name = b.item_code
		WHERE b.actual_qty < 0
		ORDER BY b.actual_qty ASC
		""",
		as_dict=True,
	)

	# Items flagged but currently non-negative
	flagged = frappe.db.sql(
		"""
		SELECT
			i.name AS item_code,
			i.item_name,
			NULL AS warehouse,
			0 AS actual_qty,
			i.custom_negative_stock_auto_enabled AS auto_flagged,
			i.custom_last_reconciliation_request AS last_requested
		FROM `tabItem` i
		WHERE i.custom_negative_stock_auto_enabled = 1
		""",
		as_dict=True,
	)
	seen = {(r["item_code"], r["warehouse"]) for r in rows}
	for f in flagged:
		if not any(r["item_code"] == f["item_code"] for r in rows):
			rows.append(f)

	return columns, rows

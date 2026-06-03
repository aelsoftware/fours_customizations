"""
Patch: ensure the Sales Invoice ``custom_sales_person`` field exists and
backfill it from ``sales_partner`` for every Sales Invoice.

Mirrors ``backfill_payment_entry_sales_person``. It supersedes the
field-population side of ``migrate_sales_partner_to_custom_sales_person``, which
could run as a no-op: that patch lives in ``post_model_sync`` but the
``custom_sales_person`` field is created by install.py via the ``after_migrate``
hook — which runs *after* patches — so its ``has_column`` guard returned early
and it is now logged as done and never re-runs.

This patch is self-contained: it creates the field first, then copies the data.
``custom_sales_person`` is a Link to **Sales Person** while ``sales_partner`` is
a Link to **Sales Partner**, so each ``sales_partner`` value is resolved to a
Sales Person by name (then ``sales_person_name``); a raw copy would leave a
broken link. Values that do not resolve are left as-is and reported.

This patch only sets ``custom_sales_person`` — it does NOT touch the Sales Team
table. Safe to run multiple times.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
	_ensure_custom_sales_person_field()

	if not frappe.db.has_column("Sales Invoice", "sales_partner"):
		# Nothing to migrate from on this site.
		return

	rows = frappe.db.sql(
		"""
		SELECT name, sales_partner
		FROM `tabSales Invoice`
		WHERE sales_partner IS NOT NULL
			AND sales_partner != ''
			AND (custom_sales_person IS NULL OR custom_sales_person = '')
		""",
		as_dict=True,
	)

	moved = 0
	missing: dict[str, int] = {}
	for row in rows:
		sp = _resolve_sales_person(row.sales_partner)
		if not sp:
			missing[row.sales_partner] = missing.get(row.sales_partner, 0) + 1
			continue
		frappe.db.set_value(
			"Sales Invoice",
			row.name,
			"custom_sales_person",
			sp,
			update_modified=False,
		)
		moved += 1

	frappe.db.commit()

	print(f"4S patch: set custom_sales_person on {moved} Sales Invoices")
	if missing:
		print("4S patch: sales_partner with no matching Sales Person (left as-is):")
		for partner, count in sorted(missing.items(), key=lambda x: -x[1]):
			print(f"   - {partner!r} on {count} Sales Invoice(s)")


def _ensure_custom_sales_person_field() -> None:
	"""Create the ``custom_sales_person`` Link field if it is missing (matching
	install.py: positioned right after ``sales_partner``)."""
	create_custom_fields(
		{
			"Sales Invoice": [
				{
					"fieldname": "custom_sales_person",
					"label": "Sales Person",
					"fieldtype": "Link",
					"options": "Sales Person",
					"insert_after": "sales_partner",
					"description": "When set, the Sales Team is automatically populated with this person at 100% allocation.",
				}
			]
		},
		update=True,
	)


def _resolve_sales_person(value: str) -> str | None:
	"""Resolve a ``sales_partner`` value to a Sales Person ``name``.

	Matches on the document name first, then falls back to ``sales_person_name``
	so display-name values still resolve to the linkable record name.
	"""
	if not value:
		return None
	direct = frappe.db.get_value("Sales Person", value, "name")
	if direct:
		return direct
	return frappe.db.get_value("Sales Person", {"sales_person_name": value}, "name")

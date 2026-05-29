"""
Patch: ensure the Payment Entry ``sales_person`` field exists (positioned right
after ``payment_type``) and backfill it from the legacy ``custom_sales_person``
field for every Payment Entry.

This supersedes ``migrate_custom_sales_person_to_sales_person``, which could run
as a no-op: that patch lives in ``post_model_sync`` but the ``sales_person``
field is created by install.py via the ``after_migrate`` hook — which runs
*after* patches. So on the migrate where it first ran the column did not exist
yet, its ``has_column`` guard returned early, and it is now logged as done and
never re-runs.

This patch is self-contained: it creates/repositions the field first, then
copies the data — so it works regardless of patch/after-migrate ordering.

Safe to run multiple times: only fills ``sales_person`` where it is empty and
``custom_sales_person`` resolves to a real Sales Person.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
	_ensure_sales_person_field()

	if not frappe.db.has_column("Payment Entry", "custom_sales_person"):
		# Nothing to migrate from on this site.
		return

	rows = frappe.db.sql(
		"""
		SELECT name, custom_sales_person
		FROM `tabPayment Entry`
		WHERE custom_sales_person IS NOT NULL
			AND custom_sales_person != ''
			AND (sales_person IS NULL OR sales_person = '')
		""",
		as_dict=True,
	)

	moved = 0
	missing: dict[str, int] = {}
	for row in rows:
		sp = _resolve_sales_person(row.custom_sales_person)
		if not sp:
			missing[row.custom_sales_person] = missing.get(row.custom_sales_person, 0) + 1
			continue
		frappe.db.set_value(
			"Payment Entry",
			row.name,
			"sales_person",
			sp,
			update_modified=False,
		)
		moved += 1

	frappe.db.commit()

	print(f"4S patch: set sales_person on {moved} Payment Entries")
	if missing:
		print("4S patch: custom_sales_person with no matching Sales Person (left as-is):")
		for person, count in sorted(missing.items(), key=lambda x: -x[1]):
			print(f"   - {person!r} on {count} Payment Entry(ies)")


def _ensure_sales_person_field() -> None:
	"""Create the ``sales_person`` Link field, or reposition it to sit right
	after ``payment_type``. ``update=True`` repositions an existing field."""
	create_custom_fields(
		{
			"Payment Entry": [
				{
					"fieldname": "sales_person",
					"label": "Sales Person",
					"fieldtype": "Link",
					"options": "Sales Person",
					"insert_after": "payment_type",
					"description": "Sales Person who collected this payment. Drives commission for their Salary Slip.",
				}
			]
		},
		update=True,
	)


def _resolve_sales_person(value: str) -> str | None:
	"""Resolve a stored ``custom_sales_person`` value to a Sales Person ``name``.

	Matches on the document name first, then falls back to ``sales_person_name``
	so display-name values still resolve to the linkable record name.
	"""
	if not value:
		return None
	direct = frappe.db.get_value("Sales Person", value, "name")
	if direct:
		return direct
	return frappe.db.get_value("Sales Person", {"sales_person_name": value}, "name")

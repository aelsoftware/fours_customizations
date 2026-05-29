"""
Patch: backfill the Sales Invoice Sales Team table from ``custom_sales_person``.

Replicates ``sales_invoice_handler._sync_custom_sales_person_to_team`` (which
runs on save) for historical invoices: for every submitted Sales Invoice that
carries a ``custom_sales_person``, the ``sales_team`` table is forced to a
SINGLE row — that person at 100% allocation, with their commission rate and the
invoice's base grand total.

Only submitted invoices (docstatus = 1) are processed: drafts self-heal on their
next save via the before_save sync, and cancelled invoices are left untouched.
Run this AFTER ``backfill_sales_invoice_custom_sales_person`` so the field is
populated first (patches.txt ordering guarantees this).

Safe to run multiple times: an invoice whose ``sales_team`` is already exactly
the expected single 100% row only has its amount/rate refreshed.
"""

import frappe
from frappe.utils import flt


def execute() -> None:
	if not frappe.db.has_column("Sales Invoice", "custom_sales_person"):
		return

	rows = frappe.db.sql(
		"""
		SELECT name, custom_sales_person, base_grand_total, grand_total
		FROM `tabSales Invoice`
		WHERE docstatus = 1
			AND custom_sales_person IS NOT NULL
			AND custom_sales_person != ''
		""",
		as_dict=True,
	)

	replaced = 0
	missing: dict[str, int] = {}
	for si in rows:
		if not frappe.db.exists("Sales Person", si.custom_sales_person):
			missing[si.custom_sales_person] = missing.get(si.custom_sales_person, 0) + 1
			continue
		if _sync_sales_team(si):
			replaced += 1

	frappe.db.commit()

	print(f"4S patch: rebuilt Sales Team on {replaced} Sales Invoices")
	if missing:
		print("4S patch: custom_sales_person with no matching Sales Person (skipped):")
		for person, count in sorted(missing.items(), key=lambda x: -x[1]):
			print(f"   - {person!r} on {count} Sales Invoice(s)")


def _sync_sales_team(si) -> bool:
	"""Force this invoice's Sales Team to a single 100% row for its
	``custom_sales_person``. Returns True when the table was (re)built, False
	when it already matched and only the amount/rate were refreshed."""
	sp = si.custom_sales_person
	allocated_amount = flt(si.base_grand_total or si.grand_total or 0)
	rate = flt(frappe.db.get_value("Sales Person", sp, "commission_rate"))

	existing = frappe.db.sql(
		"""
		SELECT name, sales_person, allocated_percentage
		FROM `tabSales Team`
		WHERE parent = %s AND parenttype = 'Sales Invoice'
		ORDER BY idx
		""",
		(si.name,),
		as_dict=True,
	)

	# Already exactly the expected single 100% row → just refresh amount/rate.
	if (
		len(existing) == 1
		and existing[0].sales_person == sp
		and flt(existing[0].allocated_percentage) == 100
	):
		frappe.db.set_value(
			"Sales Team",
			existing[0].name,
			{"allocated_amount": allocated_amount, "commission_rate": rate, "incentives": 0},
			update_modified=False,
		)
		return False

	# Otherwise replace the whole table with a single 100% row for this person,
	# mirroring the on-save behaviour.
	frappe.db.sql(
		"DELETE FROM `tabSales Team` WHERE parent = %s AND parenttype = 'Sales Invoice'",
		(si.name,),
	)
	frappe.db.sql(
		"""
		INSERT INTO `tabSales Team`
			(name, parent, parenttype, parentfield, idx, docstatus,
			 sales_person, allocated_percentage, allocated_amount, commission_rate, incentives,
			 creation, modified, modified_by, owner)
		VALUES
			(%(name)s, %(parent)s, 'Sales Invoice', 'sales_team', 1, 1,
			 %(sp)s, 100, %(allocated_amount)s, %(rate)s, 0,
			 NOW(), NOW(), %(user)s, %(user)s)
		""",
		{
			"name": frappe.generate_hash(length=10),
			"parent": si.name,
			"sp": sp,
			"allocated_amount": allocated_amount,
			"rate": rate,
			"user": frappe.session.user or "Administrator",
		},
	)
	return True

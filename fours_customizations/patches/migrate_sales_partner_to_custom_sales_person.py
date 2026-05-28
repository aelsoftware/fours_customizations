"""
Patch: migrate Sales Invoice `sales_partner` → `custom_sales_person`.

For every Sales Invoice that carries a `sales_partner`, look up a Sales Person
whose name matches (either `name` or `sales_person_name`).  When found:

  1. Set `custom_sales_person` on the invoice (direct SQL, bypasses validate).
  2. Insert a Sales Team row at 100% allocation with the Sales Person's
     commission rate — if no row for that person already exists.

`sales_partner` is left intact so historical reports still resolve.

Safe to run multiple times — guarded by an emptiness check on
`custom_sales_person` and an existence check on the Sales Team row.
"""

import frappe
from frappe.utils import flt


def execute() -> None:
	if not frappe.db.has_column("Sales Invoice", "custom_sales_person"):
		# install.py hasn't created the field yet on this site.
		return

	rows = frappe.db.sql(
		"""
		SELECT name, sales_partner, base_grand_total
		FROM `tabSales Invoice`
		WHERE sales_partner IS NOT NULL
			AND sales_partner != ''
			AND (custom_sales_person IS NULL OR custom_sales_person = '')
		""",
		as_dict=True,
	)

	matched = 0
	missing_persons: dict[str, int] = {}

	for si in rows:
		sp = _resolve_sales_person(si.sales_partner)
		if not sp:
			missing_persons[si.sales_partner] = missing_persons.get(si.sales_partner, 0) + 1
			continue

		# 1. Stamp the SI with the matched Sales Person — direct update, no docevents
		frappe.db.set_value("Sales Invoice", si.name, "custom_sales_person", sp, update_modified=False)

		# 2. Ensure Sales Team row exists with this person at 100%
		_ensure_sales_team_row(si.name, sp, si.base_grand_total)
		matched += 1

	frappe.db.commit()

	print(f"4S patch: migrated custom_sales_person on {matched} Sales Invoices")
	if missing_persons:
		print("4S patch: no matching Sales Person for the following Sales Partner names:")
		for partner, count in sorted(missing_persons.items(), key=lambda x: -x[1]):
			print(f"   - {partner!r} on {count} invoice(s)")


def _resolve_sales_person(name: str) -> str | None:
	"""Find a Sales Person whose name OR sales_person_name matches `name`."""
	if not name:
		return None
	direct = frappe.db.get_value("Sales Person", name, "name")
	if direct:
		return direct
	by_label = frappe.db.get_value("Sales Person", {"sales_person_name": name}, "name")
	return by_label


def _ensure_sales_team_row(si_name: str, sp: str, base_grand_total) -> None:
	if frappe.db.exists("Sales Team", {"parent": si_name, "parenttype": "Sales Invoice", "sales_person": sp}):
		return

	rate = flt(frappe.db.get_value("Sales Person", sp, "commission_rate"))

	# Inserting a child against a submitted parent — go straight to the table to
	# avoid mutating the parent SI document state.
	idx = (
		frappe.db.sql(
			"SELECT COALESCE(MAX(idx), 0) FROM `tabSales Team` WHERE parent = %s",
			(si_name,),
		)[0][0]
		+ 1
	)
	frappe.db.sql(
		"""
		INSERT INTO `tabSales Team`
			(name, parent, parenttype, parentfield, idx, docstatus,
			 sales_person, allocated_percentage, allocated_amount, commission_rate, incentives,
			 creation, modified, modified_by, owner)
		VALUES
			(%(name)s, %(parent)s, 'Sales Invoice', 'sales_team', %(idx)s, 1,
			 %(sp)s, 100, %(allocated_amount)s, %(rate)s, 0,
			 NOW(), NOW(), %(user)s, %(user)s)
		""",
		{
			"name": frappe.generate_hash(length=10),
			"parent": si_name,
			"idx": idx,
			"sp": sp,
			"allocated_amount": flt(base_grand_total),
			"rate": rate,
			"user": frappe.session.user or "Administrator",
		},
	)

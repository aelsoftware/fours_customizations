"""
Patch: migrate the legacy `custom_sales_person` field on Payment Entry into
the canonical `sales_person` Link field.

Safe to run multiple times — only fills `sales_person` where it's empty and
`custom_sales_person` has a value.
"""

import frappe


def execute() -> None:
	if not frappe.db.has_column("Payment Entry", "custom_sales_person"):
		# Nothing to migrate — column never existed on this site.
		return

	if not frappe.db.has_column("Payment Entry", "sales_person"):
		# The new field hasn't been created yet (install.py not run).
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
	for row in rows:
		# Only copy if the value still names a real Sales Person, else log + skip.
		if not frappe.db.exists("Sales Person", row.custom_sales_person):
			frappe.log_error(
				f"Payment Entry {row.name}: custom_sales_person '{row.custom_sales_person}' "
				"does not match a Sales Person — left unmigrated.",
				"4S migrate_custom_sales_person",
			)
			continue
		frappe.db.set_value(
			"Payment Entry",
			row.name,
			"sales_person",
			row.custom_sales_person,
			update_modified=False,
		)
		moved += 1

	frappe.db.commit()
	print(f"4S patch: migrated sales_person on {moved} Payment Entries")

"""
Patch: drop the now-unused Sales Invoice ``custom_auto_created_sales_order``
custom field.

The Sales Invoice ↔ auto-created Sales Order relationship is now carried by
native ERPNext fields (``Sales Invoice Item.sales_order`` / ``so_detail``, wired
in ``si_to_so._link_invoice_to_sales_order``), and the duplicate-creation guard keys off
the Sales Order's ``custom_source_sales_invoice``. So the invoice-side pointer
field is redundant.

Deleting the Custom Field removes it from the form/meta. The underlying column
is left in place (Frappe does not drop columns on field deletion, to avoid data
loss); it is harmless. Drop it manually if you want it gone:

    ALTER TABLE `tabSales Invoice` DROP COLUMN `custom_auto_created_sales_order`;
"""

import frappe


def execute() -> None:
	name = "Sales Invoice-custom_auto_created_sales_order"
	if frappe.db.exists("Custom Field", name):
		frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
		frappe.clear_cache(doctype="Sales Invoice")
		frappe.db.commit()
		print("4S patch: removed Sales Invoice custom_auto_created_sales_order field")

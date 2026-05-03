"""
Patch: Allow Sales Invoice Item link fields to be editable after submit.

Fields patched on Sales Invoice Item:
  - delivery_note   (Delivery Note link)
  - dn_detail       (Delivery Note Item link)
  - sales_order     (Sales Order link)
  - so_detail       (Sales Order Item link)

These are standard ERPNext fields, so we use Property Setter (not Custom Field)
to flip allow_on_submit = 1 without touching the core schema.
"""

import frappe


def execute():
    fields = [
        "delivery_note",
        "dn_detail",
        "sales_order",
        "so_detail",
    ]

    for fieldname in fields:
        _set_allow_on_submit("Sales Invoice Item", fieldname)

    frappe.db.commit()


def _set_allow_on_submit(doctype, fieldname):
    """Create or update a Property Setter that sets allow_on_submit = 1."""
    ps_name = f"{doctype}-{fieldname}-allow_on_submit"

    if frappe.db.exists("Property Setter", ps_name):
        frappe.db.set_value("Property Setter", ps_name, "value", "1")
        print(f"  Updated Property Setter: {ps_name}")
    else:
        frappe.get_doc({
            "doctype": "Property Setter",
            "name": ps_name,
            "doctype_or_field": "DocField",
            "doc_type": doctype,
            "field_name": fieldname,
            "property": "allow_on_submit",
            "property_type": "Check",
            "value": "1",
        }).insert(ignore_permissions=True)
        print(f"  Created Property Setter: {ps_name}")
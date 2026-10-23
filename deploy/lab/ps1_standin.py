"""LAB ONLY (fqpm.lab, never production): a stand-in for the lead's Phase 1 data kit (ps1.py), so ps2.py and the smoke can be
rehearsed. bench --site fqpm.lab console, then exec(open(<this file>).read(), globals()).

Creates Price List.custom_company if missing, the three company lists, copies Standard Selling rows by item family
('4S Items' -> 4S list; 'Vox Items' + 'Raw Kitchen Items' -> both bar lists; SPVN-only Vox rows to both bar lists; exact
duplicates once), re-points POS Profiles / Kit POS Locations / Selling Settings + global default / Customer Groups /
Customers / Item Defaults / draft documents to each company's list (db.set_value), and pauses the Quick Price page (Custom
Role: System Manager only). Standard Selling and SPVN stay enabled.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

frappe.set_user("Administrator")
SS, SPVN = "Standard Selling", "Selling Price Vox Nansana"
NEW = {"4S Industries Limited": "4S Selling Price", "Vox Lounge": "Vox Makindye Selling Price", "Vox Lounge Nansana": "Vox Nansana Selling Price"}
FAM = {"4S Industries Limited": ("4S Items",), "Vox Lounge": ("Vox Items", "Raw Kitchen Items"), "Vox Lounge Nansana": ("Vox Items", "Raw Kitchen Items")}

if not frappe.get_meta("Price List").has_field("custom_company"):
	create_custom_field("Price List", {"fieldname": "custom_company", "label": "Company", "fieldtype": "Link", "options": "Company", "insert_after": "currency", "in_list_view": 1, "in_standard_filter": 1})
for company, name in NEW.items():
	if not frappe.db.exists("Price List", name):
		frappe.get_doc({"doctype": "Price List", "price_list_name": name, "currency": "UGX", "selling": 1, "buying": 0, "enabled": 1, "custom_company": company}).insert()
bounds = {g: frappe.db.get_value("Item Group", g, ["lft", "rgt"]) for g in ("4S Items", "Vox Items", "Raw Kitchen Items")}


def in_family(item_code, company):
	group = frappe.db.get_value("Item", item_code, "item_group")
	lr = frappe.db.get_value("Item Group", group, ["lft", "rgt"]) if group else None
	return bool(lr) and any(bounds[g][0] <= lr[0] and lr[1] <= bounds[g][1] for g in FAM[company])


fields = ["item_code", "uom", "price_list_rate", "valid_from", "valid_upto", "customer", "batch_no", "packing_unit", "note", "reference", "currency"]
copied = 0
ss_keys = set()
for source in (SS, SPVN):
	for row in frappe.get_all("Item Price", filters={"price_list": source}, fields=fields, order_by="creation"):
		key = (row.item_code, row.uom)
		if source == SPVN and key in ss_keys:
			continue
		if source == SS:
			ss_keys.add(key)
		for company, name in NEW.items():
			if source == SPVN and company == "4S Industries Limited":
				continue
			if not in_family(row.item_code, company):
				continue
			dup = {"item_code": row.item_code, "price_list": name, "uom": row.uom, "valid_from": row.valid_from, "valid_upto": row.valid_upto, "customer": row.customer, "batch_no": row.batch_no}
			if frappe.db.exists("Item Price", {k: v for k, v in dup.items() if v} | {k: ["is", "not set"] for k, v in dup.items() if not v}):
				continue
			doc = frappe.get_doc({"doctype": "Item Price", "price_list": name, "selling": 1, "buying": 0, **{f: row.get(f) for f in fields}})
			doc.flags.ignore_permissions = True
			doc.db_insert()
			copied += 1
frappe.db.commit()
for doctype in ("POS Profile", "Kit POS Location"):
	for r in frappe.get_all(doctype, fields=["name", "company"]):
		if r.company in NEW:
			frappe.db.set_value(doctype, r.name, "selling_price_list", NEW[r.company], update_modified=False)
frappe.db.set_single_value("Selling Settings", "selling_price_list", NEW["4S Industries Limited"])
frappe.db.set_default("selling_price_list", NEW["4S Industries Limited"])
for dt in ("Customer Group",):
	for n in frappe.get_all(dt, filters={"default_price_list": SS}, pluck="name"):
		frappe.db.set_value(dt, n, "default_price_list", NEW["4S Industries Limited"], update_modified=False)
for n in frappe.get_all("Customer", filters={"default_price_list": ["in", [SS, SPVN]]}, pluck="name"):
	frappe.db.set_value("Customer", n, "default_price_list", None, update_modified=False)
for r in frappe.get_all("Item Default", filters={"default_price_list": ["in", [SS, SPVN]], "company": ["in", list(NEW)]}, fields=["name", "company"]):
	frappe.db.set_value("Item Default", r.name, "default_price_list", NEW[r.company], update_modified=False)
frappe.db.sql("delete from `tabDefaultValue` where defkey = 'selling_price_list' and defvalue in (%s, %s) and parent not in ('__default', '__global')", (SS, SPVN))
for doctype in ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "POS Invoice"):
	for r in frappe.get_all(doctype, filters={"docstatus": 0, "selling_price_list": ["in", [SS, SPVN]]}, fields=["name", "company"]):
		if r.company in NEW:
			frappe.db.set_value(doctype, r.name, "selling_price_list", NEW[r.company], update_modified=False)
role = frappe.db.get_value("Custom Role", {"page": "quick-price-update"}, "name")
if role:
	doc = frappe.get_doc("Custom Role", role)
	doc.set("roles", [{"role": "System Manager"}])
	doc.save(ignore_permissions=True)
frappe.db.commit()
frappe.cache.delete_keys("fours_quick_price:")
frappe.clear_cache()
print(f"ps1 stand-in: copied {copied} prices; lists {', '.join(NEW.values())}; consumers re-pointed; page paused")

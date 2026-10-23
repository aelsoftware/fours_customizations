"""LAB ONLY (fqpm.lab after ps2 apply; never production): fixtures for ps_ui_check.mjs. bench console exec.
A desk user ps.ui@labvox.invalid (System Manager, Accounts/Sales Manager; password in <bench>/ps-ui.json, 600), and two
pre-split Sales Invoices of Vox Lounge Nansana on Standard Selling (made while the archive is briefly re-enabled), with
rates that differ from the list: A submitted then cancelled (to amend), B submitted (to duplicate)."""

import json
import os
import secrets

import frappe
from frappe.utils import nowdate

frappe.set_user("Administrator")
frappe.local.lang = "en"  # bench console has none; frappe.locale needs it to format money in words
SS, CO = "Standard Selling", "Vox Lounge Nansana"
EMAIL = "ps.ui@labvox.invalid"
password = secrets.token_urlsafe(16)
if not frappe.db.exists("User", EMAIL):
	frappe.get_doc({"doctype": "User", "email": EMAIL, "first_name": "ps.ui", "send_welcome_email": 0}).insert()
user = frappe.get_doc("User", EMAIL)
user.add_roles("System Manager", "Accounts Manager", "Sales Manager", "Accounts User", "Sales User")
from frappe.utils.password import update_password

update_password(EMAIL, password)
items = frappe.db.sql(
	"""select ip.item_code, ip.price_list_rate from `tabItem Price` ip join `tabItem` i on i.name = ip.item_code
	join `tabItem Price` nl on nl.item_code = ip.item_code and nl.price_list = 'Vox Nansana Selling Price'
	where ip.price_list = %s and ifnull(ip.uom, i.stock_uom) = i.stock_uom and i.is_stock_item = 0 and i.disabled = 0
	and i.has_variants = 0 and ifnull(ip.valid_from, '2000-01-01') <= curdate()
	and not exists (select 1 from `tabBOM` b where b.item = i.name and b.docstatus = 1)
	order by ip.item_code limit 2""",
	SS,
	as_dict=True,
)
assert len(items) == 2, items
customer = frappe.db.get_value("Customer", {"custom_company": CO, "disabled": 0}, "name") or frappe.db.get_value(
	"Customer", {"disabled": 0}, "name"
)
frappe.db.set_value("Price List", SS, "enabled", 1)
frappe.cache.hdel("price_list_details", SS)
from fours_customizations import price_lists

price_lists.clear_caches()
names = {}
for key in ("amend", "duplicate"):
	si = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"company": CO,
			"customer": customer,
			"selling_price_list": SS,
			"posting_date": nowdate(),
			"due_date": nowdate(),
			"items": [
				{"item_code": r.item_code, "qty": 1, "rate": r.price_list_rate + 111, "price_list_rate": r.price_list_rate + 111}
				for r in items
			],
		}
	)
	si.insert()
	si.submit()
	if key == "amend":
		si.cancel()
	names[key] = si.name
frappe.db.set_value("Price List", SS, "enabled", 0)
frappe.cache.hdel("price_list_details", SS)
price_lists.clear_caches()
frappe.db.commit()
frappe.clear_cache()
path = os.path.join(frappe.utils.get_bench_path(), "ps-ui.json")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
	json.dump({"email": EMAIL, "password": password, "invoices": names, "rates": {r.item_code: r.price_list_rate + 111 for r in items}}, f)
print("ps ui seed:", names, {r.item_code: r.price_list_rate + 111 for r in items})

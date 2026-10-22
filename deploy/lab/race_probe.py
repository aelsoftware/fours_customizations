"""fqp-1 LAB ONLY: two real database connections on the multax stand-in. Connection A (a lab writer on the page) has read
the prices (its REPEATABLE READ snapshot is taken); connection B commits a new price for the same item and list; then A
saves a "new price" for that cell. With the release, A is refused (PriceChangedError) and no duplicate exists; with
assert_fresh disabled (the pre-review code) A inserts a duplicate. A always rolls back; B's row is deleted at the end.
    cd <bench>/sites && ../env/bin/python <this file> fqpm.lab <writer> <company> <price list>"""

import sys
from unittest.mock import patch

import frappe

site, user, company, price_list = sys.argv[1:5]
frappe.init(site, sites_path=".")
frappe.connect()
from frappe.database import get_db

from fours_customizations.quick_price import api

frappe.set_user(user)
item = frappe.db.sql(
	"""select i.name, i.stock_uom from tabItem i where i.disabled = 0 and i.is_sales_item = 1 and i.has_variants = 0
	and ifnull(i.sales_uom, i.stock_uom) = i.stock_uom
	and not exists (select 1 from `tabItem Price` p where p.item_code = i.name and p.price_list = %s)
	order by i.name limit 1""",
	price_list,
	as_dict=True,
)[0]
db2 = get_db(
	host=frappe.conf.db_host,
	user=frappe.conf.db_user,
	password=frappe.conf.db_password,
	cur_db_name=frappe.conf.db_name,
)
db2.connect()
row = "fqprace" + frappe.generate_hash(length=6)


def run(label, fresh_check):
	frappe.db.rollback()
	frappe.db.sql("select count(*) from `tabItem Price`")  # A's snapshot
	db2.sql(
		"""insert into `tabItem Price` (name, item_code, price_list, uom, price_list_rate, valid_from, creation, modified,
		owner, modified_by, docstatus, idx) values (%s, %s, %s, %s, 1000, curdate(), now(), now(), 'Administrator',
		'Administrator', 0, 0)""",
		(row, item.name, price_list, item.stock_uom),
	)
	db2.commit()
	change = {
		"item_code": item.name,
		"price_list": price_list,
		"uom": item.stock_uom,
		"item_price": None,
		"expected_old_rate": None,
		"new_rate": 1200,
	}
	if fresh_check:
		results = api.save_prices(company, [change], lists=[price_list])
	else:
		with patch.object(api, "assert_fresh", lambda *a: None):
			results = api.save_prices(company, [change], lists=[price_list])
	mine = frappe.db.sql(
		"select count(*) from `tabItem Price` where item_code = %s and price_list = %s for update",
		(item.name, price_list),
	)[0][0]
	print(
		f"{label}: result ok={results[0].get('ok')} {results[0].get('error') or results[0].get('status')}; "
		f"rows for the cell seen by A (locking read) = {mine}"
	)
	frappe.db.rollback()
	db2.sql("delete from `tabItem Price` where name = %s", row)
	db2.commit()
	return results[0], mine


fixed, n_fixed = run("release (assert_fresh)", True)
old, n_old = run("pre-review code (assert_fresh disabled)", False)
db2.close()
frappe.destroy()
ok = fixed.get("error") == "PriceChangedError" and n_fixed == 1 and old.get("ok") == 1 and n_old == 2
print(
	"race probe:", "PASS (release refuses; the old code would have made a duplicate)" if ok else "UNEXPECTED"
)
sys.exit(0 if ok else 1)

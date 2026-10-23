"""fqp-1 Quick Price Update: integration tests (a lab site with erpnext + fours_customizations; never production).

    bench --site <lab site> run-tests --module fours_customizations.tests.test_quick_price_update

Fixtures are the test's own (three companies, two selling lists, items, users with test roles, Item Price rules through
Custom DocPerm) and live only inside the test transaction: IntegrationTestCase rolls the class back at the end.
Invoices that make a company "use" a list are bare header/line rows (db_insert): the page reads only company, list,
date and docstatus from them.
"""

from unittest.mock import patch

import frappe
from frappe.permissions import add_permission, update_permission_property
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from fours_customizations import price_lists
from fours_customizations.quick_price import api, setup

PREFIX = "FQPT"
CO_A, CO_B, CO_C = f"{PREFIX} Alpha Ltd", f"{PREFIX} Beta Ltd", f"{PREFIX} Gamma Ltd"
LIST_SHARED, LIST_OWN, LIST_OTHER = (
	f"{PREFIX} Shared Selling",
	f"{PREFIX} Alpha Selling",
	f"{PREFIX} Other Selling",
)
READER_ROLE, WRITER_ROLE, TV_ROLE = f"{PREFIX} Price Reader", f"{PREFIX} Price Writer", "Kit TV Viewer"
WRITER, READER, NOBODY, TV, LIMITED = (
	f"fqpt.writer@{PREFIX.lower()}.invalid",
	f"fqpt.reader@{PREFIX.lower()}.invalid",
	f"fqpt.nobody@{PREFIX.lower()}.invalid",
	f"fqpt.tv@{PREFIX.lower()}.invalid",
	f"fqpt.limited@{PREFIX.lower()}.invalid",
)
ITEM_1, ITEM_2, ITEM_BOX, ITEM_DUP, ITEM_B_ONLY = (
	f"{PREFIX}-ITEM-{n}" for n in ("1", "2", "BOX", "DUP", "B")
)


def make_company(name, abbr):
	if frappe.db.exists("Company", name):
		return
	frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": name,
			"abbr": abbr,
			"default_currency": frappe.db.get_default("currency") or "UGX",
			"country": frappe.db.get_default("country") or "Uganda",
			"create_chart_of_accounts_based_on": "Standard Template",
			"chart_of_accounts": "Standard",
		}
	).insert(ignore_permissions=True)


def make_role(name):
	if not frappe.db.exists("Role", name):
		frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1}).insert(
			ignore_permissions=True
		)


def make_user(email, roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, ignore_permissions=True, force=True)
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"roles": [{"role": role} for role in roles],
		}
	).insert(ignore_permissions=True)


def make_list(name):
	if not frappe.db.exists("Price List", name):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": name,
				"selling": 1,
				"enabled": 1,
				"currency": frappe.db.get_default("currency") or "UGX",
			}
		).insert(ignore_permissions=True)


def make_item(code, stock_uom="Nos", sales_uom=None, factor=None):
	doc = {
		"doctype": "Item",
		"item_code": code,
		"item_name": f"{code} name",
		"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
		"stock_uom": stock_uom,
		"is_stock_item": 0,
		"is_sales_item": 1,
		"include_item_in_manufacturing": 0,
	}
	if sales_uom:
		doc["uoms"] = [
			{"uom": stock_uom, "conversion_factor": 1},
			{"uom": sales_uom, "conversion_factor": factor},
		]
		doc["sales_uom"] = sales_uom
	frappe.get_doc(doc).insert(ignore_permissions=True)


def make_price(item, price_list, rate, uom=None, valid_from=None):
	doc = frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item,
			"price_list": price_list,
			"price_list_rate": rate,
			"uom": uom or frappe.db.get_value("Item", item, "stock_uom"),
			"valid_from": valid_from or add_days(nowdate(), -30),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def sell(company, price_list, item_code, days_ago=1):
	"""A bare submitted Sales Invoice (header + one line): what makes a company use a list / sell an item."""
	name = frappe.generate_hash(length=12)
	frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"name": name,
			"company": company,
			"selling_price_list": price_list,
			"posting_date": add_days(nowdate(), -days_ago),
			"docstatus": 1,
		}
	).db_insert()
	frappe.get_doc(
		{
			"doctype": "Sales Invoice Item",
			"name": frappe.generate_hash(length=12),
			"parent": name,
			"parenttype": "Sales Invoice",
			"parentfield": "items",
			"item_code": item_code,
			"docstatus": 1,
		}
	).db_insert()


def set_item_price_rules():
	"""Item Price: reader = read; writer = read/write/create; Kit TV Viewer = read; System Manager = all (the site's
	existing rules stay; add_permission copies the standard rules first when there are no custom ones)."""
	for role, rights in (
		(READER_ROLE, ("read",)),
		(WRITER_ROLE, ("read", "write", "create")),
		(TV_ROLE, ("read",)),
		("System Manager", ("read", "write", "create")),
	):
		if not frappe.db.exists("Custom DocPerm", {"parent": "Item Price", "role": role, "permlevel": 0}):
			add_permission("Item Price", role, 0)
		for right in rights:
			update_permission_property("Item Price", role, 0, right, 1, validate=False)
	frappe.clear_cache(doctype="Item Price")


class TestQuickPriceUpdate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if price_lists.has_company_field():  # a site after the price list split's data step: fe153b5's world for the class
			frappe.db.sql(f"update `tabPrice List` set `{price_lists.FIELD}` = null")
			price_lists.clear_caches()
		for role in (READER_ROLE, WRITER_ROLE, TV_ROLE):
			make_role(role)
		make_company(CO_A, "FQTA")
		make_company(CO_B, "FQTB")
		make_company(CO_C, "FQTC")
		for name in (LIST_SHARED, LIST_OWN, LIST_OTHER):
			make_list(name)
		for code in (ITEM_1, ITEM_2, ITEM_DUP, ITEM_B_ONLY):
			make_item(code)
		make_item(ITEM_BOX, stock_uom="Nos", sales_uom="Box", factor=12)
		set_item_price_rules()
		# the site's default selling list is every company's fallback list: make it the shared one for the class
		frappe.db.set_single_value("Selling Settings", "selling_price_list", LIST_SHARED)
		make_user(WRITER, [WRITER_ROLE])
		make_user(READER, [READER_ROLE])
		make_user(NOBODY, ["Desk User"] if frappe.db.exists("Role", "Desk User") else [])
		make_user(TV, [TV_ROLE])
		make_user(LIMITED, [WRITER_ROLE])
		frappe.get_doc(
			{"doctype": "User Permission", "user": LIMITED, "allow": "Company", "for_value": CO_B}
		).insert(ignore_permissions=True)
		setup.sync_access(verbose=False)

		# A uses its own list and the shared one; B and C sell with the shared list only.
		for item in (ITEM_1, ITEM_2, ITEM_BOX, ITEM_DUP):
			sell(CO_A, LIST_SHARED, item)
		sell(CO_A, LIST_OWN, ITEM_1)
		sell(CO_B, LIST_SHARED, ITEM_1)
		sell(CO_B, LIST_SHARED, ITEM_B_ONLY)
		# C stocks ITEM_2 (Item Default) and nothing on invoices of the year but one old sale of the list.
		sell(CO_C, LIST_SHARED, ITEM_B_ONLY, days_ago=100)
		# outside the windows: B sold ITEM_2 400 days ago, C sold with LIST_OTHER 200 days ago
		sell(CO_B, LIST_SHARED, ITEM_2, days_ago=400)
		sell(CO_C, LIST_OTHER, ITEM_B_ONLY, days_ago=200)
		item2 = frappe.get_doc("Item", ITEM_2)
		item2.append("item_defaults", {"company": CO_C})
		item2.save(ignore_permissions=True)

		cls.p1_shared = make_price(ITEM_1, LIST_SHARED, 1000)
		cls.p1_own = make_price(ITEM_1, LIST_OWN, 900)
		cls.p2_shared = make_price(ITEM_2, LIST_SHARED, 500)
		cls.pbox_nos = make_price(ITEM_BOX, LIST_SHARED, 100, uom="Nos")
		first = frappe.get_doc("Item Price", make_price(ITEM_DUP, LIST_SHARED, 50))
		dup = frappe.copy_doc(first)
		dup.name = frappe.generate_hash(length=10)
		dup.price_list_rate = 55
		dup.db_insert()  # ERPNext refuses a duplicate on insert: a legacy duplicate as found on sites

	def setUp(self):
		api.clear_cache()
		frappe.set_user("Administrator")
		frappe.db.savepoint("fqpt_case")  # every test starts from the class fixtures

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="fqpt_case")
		api.clear_cache()

	# -- helpers --------------------------------------------------------------------------------------------------

	def items(self, user, company=CO_A, **kwargs):
		frappe.set_user(user)
		try:
			return api.get_items(company, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def row(self, data, code):
		return next(item for item in data["items"] if item["item_code"] == code)

	def save(self, user, changes, company=CO_A, **kwargs):
		frappe.set_user(user)
		try:
			return api.save_prices(company, changes, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def change(self, item, price_list, new_rate, item_price=None, expected=None, uom=None):
		return {
			"item_code": item,
			"price_list": price_list,
			"uom": uom or "Nos",
			"item_price": item_price,
			"expected_old_rate": expected,
			"new_rate": new_rate,
		}

	def rate(self, name):
		return flt(frappe.db.get_value("Item Price", name, "price_list_rate"))

	# -- access -----------------------------------------------------------------------------------------------------

	def test_page_roles_follow_item_price_read(self):
		roles = api.page_roles()
		self.assertIn(READER_ROLE, roles)
		self.assertIn(WRITER_ROLE, roles)
		self.assertIn("System Manager", roles)
		self.assertNotIn(TV_ROLE, roles)
		custom = frappe.get_doc("Custom Role", {"page": api.PAGE})
		self.assertEqual(
			sorted(r.role for r in custom.roles), sorted(r for r in roles if frappe.db.exists("Role", r))
		)
		self.assertFalse(setup.sync_access(verbose=False)["Custom Role"]["added"])  # idempotent

	def test_page_roles_rules(self):
		with patch.object(api, "price_read_roles", return_value=[READER_ROLE]):
			self.assertEqual(api.page_roles(), sorted([READER_ROLE, "System Manager"]))
		own = f"{PREFIX} Own Reader"
		make_role(own)
		frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": "Item Price",
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": own,
				"permlevel": 0,
				"if_owner": 1,
				"read": 1,
			}
		).insert(ignore_permissions=True)
		self.assertNotIn(own, api.price_read_roles())  # only-own read is not read of every price

	def test_sync_removes_roles_without_read(self):
		make_role(f"{PREFIX} No Read")
		custom = frappe.get_doc("Custom Role", {"page": api.PAGE})
		custom.append("roles", {"role": f"{PREFIX} No Read"})
		custom.save(ignore_permissions=True)
		change = setup.sync_access(verbose=False)["Custom Role"]
		self.assertEqual(change["removed"], [f"{PREFIX} No Read"])
		roles = [r.role for r in frappe.get_doc("Custom Role", {"page": api.PAGE}).roles]
		self.assertNotIn(f"{PREFIX} No Read", roles)
		self.assertIn(READER_ROLE, roles)

	def test_page_role_without_item_price_read_gets_no_data(self):
		# an admin may add a role to the page by hand: without Item Price read it still gets nothing
		role = f"{PREFIX} No Read"
		make_role(role)
		custom = frappe.get_doc("Custom Role", {"page": api.PAGE})
		custom.append("roles", {"role": role})
		custom.save(ignore_permissions=True)
		make_user(f"fqpt.noread@{PREFIX.lower()}.invalid", [role])
		frappe.set_user(f"fqpt.noread@{PREFIX.lower()}.invalid")
		self.assertTrue(api.page_permitted())
		with self.assertRaises(frappe.PermissionError):
			api.get_items(CO_A)

	def test_no_item_price_read_no_data(self):
		for user in (NOBODY, TV):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.items(user)
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError):
				api.get_context()
			frappe.set_user("Administrator")

	def test_company_access(self):
		frappe.set_user(LIMITED)
		ctx = api.get_context()
		self.assertEqual(ctx["companies"], [CO_B])
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError):
			self.items(LIMITED, company=CO_A)
		with self.assertRaises(frappe.PermissionError):
			self.save(LIMITED, [self.change(ITEM_1, LIST_SHARED, 1100, self.p1_shared, 1000)], company=CO_A)
		self.assertEqual(self.rate(self.p1_shared), 1000)
		self.assertTrue(self.items(LIMITED, company=CO_B)["items"])
		frappe.set_user(READER)
		self.assertTrue({CO_A, CO_B, CO_C} <= set(api.get_context()["companies"]))

	def test_price_list_user_permission(self):
		frappe.get_doc(
			{"doctype": "User Permission", "user": READER, "allow": "Price List", "for_value": LIST_OWN}
		).insert(ignore_permissions=True)
		try:
			data = self.items(READER, lists=[LIST_SHARED])
			self.assertEqual([l["price_list"] for l in data["lists"]], [LIST_OWN])
		finally:
			frappe.db.delete("User Permission", {"user": READER, "allow": "Price List"})
			frappe.cache.hdel("user_permissions", READER)

	def test_item_user_permission_limits_reads(self):
		frappe.get_doc(
			{"doctype": "User Permission", "user": READER, "allow": "Item", "for_value": ITEM_1}
		).insert(ignore_permissions=True)
		try:
			codes = {i["item_code"] for i in self.items(READER)["items"]}
			self.assertEqual(codes, {ITEM_1})
		finally:
			frappe.db.delete("User Permission", {"user": READER, "allow": "Item"})
			frappe.cache.hdel("user_permissions", READER)

	# -- lists, shared notice --------------------------------------------------------------------------------------

	def test_price_list_split_dark_and_active(self):
		"""fe153b5 behaviour while no Price List has a company; with companies set the page shows each company's own
		lists (no Selling Settings fallback, nothing 'shared') and hides other companies' lists from restricted users."""
		self.assertFalse(price_lists.split_active())
		self.assertIn(LIST_SHARED, api.company_lists(CO_B, api.list_usage()))
		if not price_lists.has_company_field():
			self.skipTest("Price List.custom_company (the split's data step) is not on this site")
		frappe.db.set_value("Price List", LIST_OWN, price_lists.FIELD, CO_A, update_modified=False)
		frappe.db.set_value("Price List", LIST_OTHER, price_lists.FIELD, CO_B, update_modified=False)
		price_lists.clear_caches()
		try:
			usage = api.list_usage()
			self.assertEqual(api.company_lists(CO_A, usage), [LIST_OWN])
			self.assertEqual(api.company_lists(CO_B, usage), [LIST_OTHER])
			self.assertEqual(api.company_lists(CO_C, usage), [])
			self.assertEqual(api.users_of(LIST_OWN, usage), [CO_A])
			names = {row.name for row in api.selectable_lists(LIMITED)}  # Company row: B only
			self.assertIn(LIST_OTHER, names)
			self.assertIn(LIST_SHARED, names)  # no company: everyone's
			self.assertNotIn(LIST_OWN, names)
			frappe.set_user(LIMITED)
			data = api.get_items(CO_B, lists=[LIST_OWN, LIST_SHARED])
			self.assertEqual([l["price_list"] for l in data["lists"]], [LIST_OTHER, LIST_SHARED])
		finally:
			frappe.set_user("Administrator")
			for name in (LIST_OWN, LIST_OTHER):
				frappe.db.set_value("Price List", name, price_lists.FIELD, None, update_modified=False)
			price_lists.clear_caches()

	def test_company_lists_and_added_list(self):
		data = self.items(READER)
		names = [l["price_list"] for l in data["lists"]]
		self.assertEqual(names[:2], [LIST_SHARED, LIST_OWN])  # most used first
		self.assertNotIn(LIST_OTHER, names)
		data = self.items(READER, lists=[LIST_OTHER, "No Such List"])
		names = [l["price_list"] for l in data["lists"]]
		self.assertIn(LIST_OTHER, names)
		self.assertNotIn("No Such List", names)
		other = next(l for l in data["lists"] if l["price_list"] == LIST_OTHER)
		self.assertEqual(
			(other["own"], other["used_by"], other["notice"]), (0, [], None)
		)  # C's use is 200 days old
		self.assertNotIn(LIST_OTHER, [l["price_list"] for l in self.items(READER, company=CO_C)["lists"]])

	def test_shared_list_notice(self):
		data = self.items(READER)
		shared = next(l for l in data["lists"] if l["price_list"] == LIST_SHARED)
		self.assertEqual(shared["used_by"], sorted([CO_A, CO_B, CO_C]))
		for company in (CO_A, CO_B, CO_C):
			self.assertIn(company, shared["notice"])
		self.assertIn("a change applies to all of them", shared["notice"])
		own = next(l for l in data["lists"] if l["price_list"] == LIST_OWN)
		self.assertEqual(own["used_by"], [CO_A])
		self.assertIsNone(own["notice"])
		# from B's side the notice still names A and C
		shared_b = next(
			l for l in self.items(READER, company=CO_B)["lists"] if l["price_list"] == LIST_SHARED
		)
		self.assertIn(CO_A, shared_b["notice"])

	def test_also_sold_by(self):
		data = self.items(READER)
		self.assertEqual(self.row(data, ITEM_1)["also_sold_by"], {LIST_SHARED: [CO_B]})  # B's invoice line
		self.assertEqual(self.row(data, ITEM_2)["also_sold_by"], {LIST_SHARED: [CO_C]})  # C's Item Default
		self.assertEqual(self.row(data, ITEM_BOX)["also_sold_by"], {})
		# B stocks ITEM_BOX in its warehouse: a Bin is enough
		warehouse = frappe.db.get_value("Warehouse", {"company": CO_B, "is_group": 0}, "name")
		frappe.get_doc(
			{
				"doctype": "Bin",
				"item_code": ITEM_BOX,
				"warehouse": warehouse,
				"actual_qty": 3,
				"stock_uom": "Nos",
			}
		).db_insert()
		api.clear_cache()
		self.assertEqual(self.row(self.items(READER), ITEM_BOX)["also_sold_by"], {LIST_SHARED: [CO_B]})

	# -- items ------------------------------------------------------------------------------------------------------

	def test_company_items_and_filters(self):
		codes = {i["item_code"] for i in self.items(READER)["items"]}
		self.assertTrue({ITEM_1, ITEM_2, ITEM_BOX, ITEM_DUP} <= codes)
		self.assertNotIn(ITEM_B_ONLY, codes)
		codes = {i["item_code"] for i in self.items(READER, all_sales_items=1, search=PREFIX)["items"]}
		self.assertIn(ITEM_B_ONLY, codes)
		codes = {i["item_code"] for i in self.items(READER, search=f"{PREFIX}-ITEM-BOX")["items"]}
		self.assertEqual(codes, {ITEM_BOX})
		# only without a price: ITEM_2 has no price in A's own list, ITEM_1 has both
		codes = {i["item_code"] for i in self.items(READER, only_unpriced=1)["items"]}
		self.assertIn(ITEM_2, codes)
		self.assertNotIn(ITEM_1, codes)

	def test_stock_per_company_warehouse(self):
		warehouse = frappe.db.get_value("Warehouse", {"company": CO_A, "is_group": 0}, "name")
		frappe.get_doc(
			{
				"doctype": "Bin",
				"item_code": ITEM_1,
				"warehouse": warehouse,
				"actual_qty": 7,
				"stock_uom": "Nos",
			}
		).db_insert()
		data = self.items(READER)
		self.assertEqual(self.row(data, ITEM_1)["stock"], {warehouse: 7})
		self.assertIn(warehouse, data["warehouses"])

	def test_page_length_capped(self):
		self.assertEqual(self.items(READER, page_length=500)["page_length"], api.MAX_PAGE_LENGTH)

	# -- per-cell permission ----------------------------------------------------------------------------------------

	def test_cell_editable_reader_vs_writer(self):
		reader = self.row(self.items(READER), ITEM_1)["prices"][LIST_SHARED]
		self.assertEqual((reader["editable"], reader["rate"]), (0, 1000))
		self.assertTrue(reader["lock_reason"])
		writer = self.row(self.items(WRITER), ITEM_1)["prices"][LIST_SHARED]
		self.assertEqual((writer["editable"], writer["item_price"]), (1, self.p1_shared))
		new_cell = self.row(self.items(WRITER), ITEM_2)["prices"][LIST_OWN]
		self.assertEqual((new_cell["editable"], new_cell["item_price"], new_cell["rate"]), (1, None, None))
		reader_new = self.row(self.items(READER), ITEM_2)["prices"][LIST_OWN]
		self.assertEqual(
			(reader_new["editable"], reader_new["lock_reason"]), (0, "You may not create prices.")
		)

	def test_reader_cannot_save(self):
		results = self.save(READER, [self.change(ITEM_1, LIST_SHARED, 1100, self.p1_shared, 1000)])
		self.assertEqual(results[0]["ok"], 0)
		self.assertEqual(results[0]["error"], "PermissionError")
		self.assertEqual(self.rate(self.p1_shared), 1000)
		self.assertFalse(frappe.db.exists(api.LOG_DOCTYPE, {"item_code": ITEM_1}))

	# -- save + log -------------------------------------------------------------------------------------------------

	def test_save_writes_item_price_and_log(self):
		results = self.save(
			WRITER, [self.change(ITEM_1, LIST_SHARED, 1100, self.p1_shared, 1000)], reason="supplier price up"
		)
		self.assertEqual((results[0]["ok"], results[0]["status"]), (1, "changed"), results)
		self.assertEqual(self.rate(self.p1_shared), 1100)
		self.assertEqual(frappe.db.get_value("Item Price", self.p1_shared, "modified_by"), WRITER)
		log = frappe.get_doc(api.LOG_DOCTYPE, results[0]["log"])
		self.assertEqual(
			(log.company, log.item_code, log.price_list, log.uom, log.old_rate, log.new_rate, log.item_price),
			(CO_A, ITEM_1, LIST_SHARED, "Nos", 1000, 1100, self.p1_shared),
		)
		self.assertEqual(
			(log.user, log.reason, log.source, log.action),
			(WRITER, "supplier price up", "Quick Price Update", "Changed"),
		)
		self.assertTrue(log.timestamp)
		self.assertEqual(log.shared_with, f"{CO_B}, {CO_C}")

	def test_version_when_track_changes(self):
		if not frappe.get_meta("Item Price").track_changes:
			self.skipTest("Item Price track_changes is off on this site")
		with patch.object(frappe, "in_test", False):
			results = self.save(WRITER, [self.change(ITEM_1, LIST_SHARED, 1050, self.p1_shared, 1000)])
		self.assertEqual(results[0]["ok"], 1, results)
		self.assertTrue(
			frappe.db.exists(
				"Version", {"ref_doctype": "Item Price", "docname": self.p1_shared, "owner": WRITER}
			)
		)

	def test_unchanged_is_not_logged(self):
		results = self.save(WRITER, [self.change(ITEM_1, LIST_SHARED, 1000, self.p1_shared, 1000)])
		self.assertEqual((results[0]["ok"], results[0]["status"]), (1, "unchanged"))
		self.assertFalse(frappe.db.exists(api.LOG_DOCTYPE, {"item_price": self.p1_shared}))

	def test_one_refusal_does_not_undo_the_others(self):
		results = self.save(
			WRITER,
			[
				self.change(ITEM_1, LIST_SHARED, 1100, self.p1_shared, 1000),
				self.change(ITEM_2, LIST_SHARED, 600, self.p2_shared, 499),  # stale
			],
		)
		self.assertEqual([r["ok"] for r in results], [1, 0])
		self.assertEqual((self.rate(self.p1_shared), self.rate(self.p2_shared)), (1100, 500))
		self.assertEqual(frappe.db.count(api.LOG_DOCTYPE, {"item_code": ["in", [ITEM_1, ITEM_2]]}), 1)

	def test_stale_price_refused(self):
		results = self.save(WRITER, [self.change(ITEM_2, LIST_SHARED, 550, self.p2_shared, 450)])
		self.assertEqual((results[0]["ok"], results[0]["error"]), (0, "PriceChangedError"))
		self.assertIn("changed by someone else", results[0]["message"])
		# a newer dated price makes the old record no longer the effective one
		make_price(ITEM_2, LIST_SHARED, 520, valid_from=nowdate())
		results = self.save(WRITER, [self.change(ITEM_2, LIST_SHARED, 550, self.p2_shared, 500)])
		self.assertEqual((results[0]["ok"], results[0]["error"]), (0, "PriceChangedError"))
		self.assertEqual(self.rate(self.p2_shared), 500)
		# a price created meanwhile in the cell's unit refuses the "new price" of the page
		make_price(ITEM_1, LIST_OTHER, 10)
		results = self.save(WRITER, [self.change(ITEM_1, LIST_OTHER, 12)], lists=[LIST_OTHER])
		self.assertEqual((results[0]["ok"], results[0]["error"]), (0, "PriceChangedError"))

	def test_large_change_needs_confirm(self):
		results = self.save(WRITER, [self.change(ITEM_1, LIST_SHARED, 1400, self.p1_shared, 1000)])
		self.assertEqual((results[0]["ok"], results[0]["error"]), (0, "LargeChangeError"))
		self.assertEqual(self.rate(self.p1_shared), 1000)
		results = self.save(WRITER, [self.change(ITEM_1, LIST_SHARED, 1300, self.p1_shared, 1000)])
		self.assertEqual(results[0]["ok"], 1, results)  # exactly 30% is not "more than 30%"
		results = self.save(WRITER, [self.change(ITEM_2, LIST_SHARED, 300, self.p2_shared, 500)])
		self.assertEqual(results[0]["error"], "LargeChangeError")  # a drop counts too
		results = self.save(
			WRITER, [self.change(ITEM_2, LIST_SHARED, 300, self.p2_shared, 500)], confirm_large=1
		)
		self.assertEqual(results[0]["ok"], 1, results)
		self.assertEqual(self.rate(self.p2_shared), 300)

	def test_max_changes(self):
		changes = [
			self.change(ITEM_1, LIST_SHARED, 1000 + n, self.p1_shared, 1000)
			for n in range(api.MAX_CHANGES + 1)
		]
		with self.assertRaises(frappe.ValidationError):
			self.save(WRITER, changes)
		self.assertEqual(self.rate(self.p1_shared), 1000)
		results = self.save(WRITER, changes[: api.MAX_CHANGES])
		self.assertEqual(len(results), api.MAX_CHANGES)

	def test_new_price_is_created(self):
		results = self.save(WRITER, [self.change(ITEM_2, LIST_OWN, 700)])
		self.assertEqual((results[0]["ok"], results[0]["status"]), (1, "created"), results)
		doc = frappe.get_doc("Item Price", results[0]["item_price"])
		self.assertEqual(
			(doc.item_code, doc.price_list, doc.uom, doc.price_list_rate, str(doc.valid_from), doc.owner),
			(ITEM_2, LIST_OWN, "Nos", 700, nowdate(), WRITER),
		)
		log = frappe.get_doc(api.LOG_DOCTYPE, results[0]["log"])
		self.assertEqual((log.action, log.old_rate, log.new_rate), ("Created", 0, 700))
		self.assertEqual(self.row(self.items(READER), ITEM_2)["prices"][LIST_OWN]["item_price"], doc.name)

	def test_list_not_on_page_refused(self):
		results = self.save(WRITER, [self.change(ITEM_1, LIST_OTHER, 12)])
		self.assertEqual((results[0]["ok"], results[0]["error"]), (0, "PermissionError"))

	def test_bad_rate_refused(self):
		for bad in (0, -5, "abc", None, 1e13):
			results = self.save(
				WRITER, [self.change(ITEM_1, LIST_SHARED, bad, self.p1_shared, 1000)], confirm_large=1
			)
			self.assertEqual(results[0]["ok"], 0, bad)
			self.assertIn("Enter a price above 0", results[0]["message"])
		self.assertEqual(self.rate(self.p1_shared), 1000)

	# -- duplicates, units -------------------------------------------------------------------------------------------

	def test_duplicate_prices(self):
		cell = self.row(self.items(WRITER), ITEM_DUP)["prices"][LIST_SHARED]
		self.assertEqual((cell["duplicate"], cell["editable"]), (1, 0))
		self.assertTrue(self.row(self.items(WRITER), ITEM_DUP)["warnings"])
		results = self.save(
			WRITER, [self.change(ITEM_DUP, LIST_SHARED, 60, cell["item_price"], cell["rate"])]
		)
		self.assertEqual(results[0]["ok"], 0)
		self.assertIn("duplicates", results[0]["message"])

	def test_uom_factor(self):
		row = self.row(self.items(WRITER), ITEM_BOX)
		self.assertEqual((row["uom"], row["stock_uom"], row["factor"]), ("Box", "Nos", 12))
		cell = row["prices"][LIST_SHARED]
		# no Box record: a new invoice in Box fetches the Nos price x 12
		self.assertEqual(
			(cell["item_price"], cell["rate"], cell["record_uom"], cell["derived_from"]),
			(None, 1200, "Box", self.pbox_nos),
		)
		results = self.save(WRITER, [self.change(ITEM_BOX, LIST_SHARED, 1250, None, 1200, uom="Box")])
		self.assertEqual((results[0]["ok"], results[0]["status"]), (1, "created"), results)
		self.assertEqual(frappe.db.get_value("Item Price", results[0]["item_price"], "uom"), "Box")
		cell = self.row(self.items(WRITER), ITEM_BOX)["prices"][LIST_SHARED]
		self.assertEqual((cell["item_price"], cell["rate"]), (results[0]["item_price"], 1250))
		self.assertEqual(self.rate(self.pbox_nos), 100)  # the Nos price is untouched
		results = self.save(WRITER, [self.change(ITEM_BOX, LIST_SHARED, 99, None, 100, uom="Kg")])
		self.assertEqual(results[0]["ok"], 0)  # not a unit of the item
		self.assertIn("is not a unit of", results[0]["message"])

	def test_blank_unit_record_is_a_stock_unit_price(self):
		# a legacy record without a unit, newer than the Nos one: ERPNext reads it as the stock-unit price x factor
		blank = frappe.get_doc(
			{
				"doctype": "Item Price",
				"name": frappe.generate_hash(length=10),
				"item_code": ITEM_BOX,
				"price_list": LIST_SHARED,
				"price_list_rate": 110,
				"uom": None,
				"valid_from": add_days(nowdate(), -5),
			}
		)
		blank.db_insert()
		cell = self.row(self.items(WRITER), ITEM_BOX)["prices"][LIST_SHARED]
		self.assertEqual((cell["item_price"], cell["rate"], cell["derived_from"]), (None, 1320, blank.name))
		# a client sending the blank record as the Box price is refused
		results = self.save(WRITER, [self.change(ITEM_BOX, LIST_SHARED, 1300, blank.name, 110, uom="Box")])
		self.assertEqual((results[0]["ok"], results[0]["error"]), (0, "PriceChangedError"))
		self.assertEqual(self.rate(blank.name), 110)

	def test_concurrent_commit_is_refused(self):
		# another user's committed change is invisible to this request's snapshot but not to the locking read
		real_sql = frappe.db.sql

		def sql(query, *args, **kwargs):
			rows = real_sql(query, *args, **kwargs)
			if isinstance(query, str) and query.endswith(" for update") and "`tabItem Price`" in query:
				rows = (*rows, ("concurrent-row", nowdate(), 1))
			return rows

		with patch.object(frappe.db, "sql", side_effect=sql):
			results = self.save(WRITER, [self.change(ITEM_2, LIST_OWN, 700)])
		self.assertEqual((results[0]["ok"], results[0]["error"]), (0, "PriceChangedError"))
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": ITEM_2, "price_list": LIST_OWN}))

	def test_deadlock_aborts_the_whole_save(self):
		real_save_one, real_rollback = api.save_one, frappe.db.rollback
		calls = []

		def save_one(ctx, change, *args):
			calls.append(change.item_code)
			if len(calls) == 2:
				raise frappe.QueryDeadlockError("deadlock")
			return real_save_one(ctx, change, *args)

		def rollback(save_point=None, **kwargs):  # a whole rollback here = back to the test's start
			return real_rollback(save_point=save_point or "fqpt_case", **kwargs)

		with (
			patch.object(api, "save_one", side_effect=save_one),
			patch.object(frappe.db, "rollback", side_effect=rollback),
		):
			results = self.save(
				WRITER,
				[
					self.change(ITEM_2, LIST_SHARED, 510, self.p2_shared, 500),
					self.change(ITEM_1, LIST_SHARED, 1010, self.p1_shared, 1000),
				],
			)
		self.assertEqual(calls, [ITEM_1, ITEM_2])  # one locking order: by item
		self.assertEqual(
			[(r["idx"], r["ok"], r["error"]) for r in results], [(1, 0, "Aborted"), (2, 0, "Aborted")]
		)
		self.assertEqual((self.rate(self.p1_shared), self.rate(self.p2_shared)), (1000, 500))

	def test_price_created_meanwhile_in_derived_cell(self):
		# the page showed 1,200 derived from the Nos price; meanwhile someone saved a Box price of 1,200
		make_price(ITEM_BOX, LIST_SHARED, 1200, uom="Box")
		results = self.save(WRITER, [self.change(ITEM_BOX, LIST_SHARED, 1250, None, 1200, uom="Box")])
		self.assertEqual((results[0]["ok"], results[0]["error"]), (0, "PriceChangedError"))
		self.assertEqual(frappe.db.count("Item Price", {"item_code": ITEM_BOX, "uom": "Box"}), 1)

	def test_failure_after_the_write_is_rolled_back(self):
		# an unexpected error after doc.save() (here: writing the log) undoes that change only
		with (
			patch.object(api, "now_datetime", side_effect=RuntimeError("boom")),
			patch("frappe.log_error") as log_error,
		):
			results = self.save(WRITER, [self.change(ITEM_1, LIST_SHARED, 1100, self.p1_shared, 1000)])
		self.assertEqual((results[0]["ok"], results[0]["error"]), (0, "RuntimeError"))
		self.assertTrue(log_error.called)
		self.assertEqual(self.rate(self.p1_shared), 1000)

	def test_large_change_against_derived_price(self):
		# a new Box record is judged against the derived price a new invoice fetches today (100 x 12)
		results = self.save(WRITER, [self.change(ITEM_BOX, LIST_SHARED, 2000, None, 1200, uom="Box")])
		self.assertEqual((results[0]["ok"], results[0]["error"]), (0, "LargeChangeError"))
		results = self.save(
			WRITER, [self.change(ITEM_BOX, LIST_SHARED, 2000, None, 1200, uom="Box")], confirm_large=1
		)
		self.assertEqual((results[0]["ok"], results[0]["status"]), (1, "created"), results)
		# a first price ever (nothing to compare with) needs no confirmation
		results = self.save(WRITER, [self.change(ITEM_2, LIST_OWN, 99999)])
		self.assertEqual(results[0]["ok"], 1, results)

	# -- the audit log ------------------------------------------------------------------------------------------------

	def test_log_permissions(self):
		name = self.save(WRITER, [self.change(ITEM_1, LIST_SHARED, 1100, self.p1_shared, 1000)])[0]["log"]
		log = frappe.get_doc(api.LOG_DOCTYPE, name)
		for user, can_read in ((READER, True), (WRITER, True), (TV, True), (NOBODY, False)):
			self.assertEqual(
				bool(frappe.has_permission(api.LOG_DOCTYPE, "read", doc=log, user=user)), can_read, user
			)
			frappe.set_user(user)
			self.assertEqual(name in frappe.get_list(api.LOG_DOCTYPE, pluck="name"), can_read, user)
			frappe.set_user("Administrator")
		for ptype in ("write", "delete"):
			self.assertFalse(frappe.has_permission(api.LOG_DOCTYPE, ptype, doc=log, user=WRITER))
		frappe.set_user(WRITER)
		log = frappe.get_doc(api.LOG_DOCTYPE, name)
		log.reason = "edited"
		with self.assertRaises(frappe.PermissionError):
			log.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{
					"doctype": api.LOG_DOCTYPE,
					"company": CO_A,
					"item_code": ITEM_1,
					"price_list": LIST_SHARED,
					"new_rate": 1,
					"user": WRITER,
					"timestamp": frappe.utils.now_datetime(),
				}
			).insert(ignore_permissions=True)
		frappe.set_user("Administrator")

	def test_deleting_item_price_keeps_log(self):
		name = self.save(WRITER, [self.change(ITEM_1, LIST_OWN, 950, self.p1_own, 900)])[0]["log"]
		frappe.delete_doc("Item Price", self.p1_own, ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(api.LOG_DOCTYPE, name, "item_price"), self.p1_own)

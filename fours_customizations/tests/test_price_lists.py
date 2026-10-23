"""Price list split (ps-fours-2): integration tests of fours_customizations/price_lists.py and the code that follows it.

    bench --site <lab site> run-tests --module fours_customizations.tests.test_price_lists

Needs a multax-shaped lab site (companies '4S Industries Limited', 'Vox Lounge', 'Vox Lounge Nansana', Item Groups
'4S Items', 'Vox Items', 'Raw Kitchen Items'); never production. The Custom Field Price List.custom_company is the data
step's, Company.custom_default_selling_price_list the release's (after_migrate): when the site lacks them, setUpClass
creates them first (DDL, committed: the only things that outlive the test).
Everything else is the test's own and lives inside the test transaction (IntegrationTestCase rolls the class back).
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field
from frappe.permissions import add_permission, update_permission_property
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from fours_customizations import landed_cost_handler, price_lists
from fours_customizations.quick_price import api

P = "PST"
CO_4S, CO_VXM, CO_VXN, CO_ML = "4S Industries Limited", "Vox Lounge", "Vox Lounge Nansana", "Multax (U) Limited"
L_4S, L_VXM, L_VXN = f"{P} 4S Selling", f"{P} Makindye Selling", f"{P} Nansana Selling"
L_OLD, L_BUY, L_USD = f"{P} Old Selling", f"{P} Shared Buying", f"{P} 4S Dollar"
WRITER_ROLE, READER_ROLE = f"{P} Price Writer", f"{P} Price Reader"
U = {
	key: f"pst.{key}@pst.invalid"
	for key in ("s4", "vxm", "vxn", "vxm_item_rows", "free", "ml", "reader4s", "sm_vxm", "sm", "ip_vxm")
}
I_4S, I_VOX, I_RAW, I_OTHER = (f"{P}-ITEM-{n}" for n in ("4S", "VOX", "RAW", "OTHER"))
CUSTOMER = f"{P} Customer"


def leaf_group(inside=None, outside=()):
	"""A leaf Item Group inside `inside`'s subtree (or outside every group in `outside`)."""
	if inside:
		lft, rgt = frappe.db.get_value("Item Group", inside, ["lft", "rgt"])
		if frappe.db.get_value("Item Group", inside, "is_group") == 0:
			return inside
		return frappe.db.get_value("Item Group", {"lft": [">", lft], "rgt": ["<", rgt], "is_group": 0}, "name")
	for row in frappe.get_all("Item Group", filters={"is_group": 0}, fields=["name", "lft", "rgt"], order_by="lft"):
		if not any(
			frappe.db.get_value("Item Group", g, "lft") <= row.lft and row.rgt <= frappe.db.get_value("Item Group", g, "rgt")
			for g in outside
		):
			return row.name


def make_role(name):
	if not frappe.db.exists("Role", name):
		frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1}).insert(ignore_permissions=True)


def make_user(email, roles, companies=(), item_only=(), price_only=()):
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
	for company in companies:
		user_permission(email, company)
	for company in item_only:
		user_permission(email, company, applicable_for="Item")
	for company in price_only:  # a Company row scoped to Item Price: Frappe's own link checks ignore it elsewhere
		user_permission(email, company, applicable_for="Item Price")


def user_permission(user, company, applicable_for=None):
	doc = {"doctype": "User Permission", "user": user, "allow": "Company", "for_value": company}
	if applicable_for:
		doc.update(apply_to_all_doctypes=0, applicable_for=applicable_for)
	frappe.get_doc(doc).insert(ignore_permissions=True)


def make_list(name, selling=1, currency="UGX"):
	if not frappe.db.exists("Price List", name):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": name,
				"selling": selling,
				"buying": int(not selling),
				"enabled": 1,
				"currency": currency,
			}
		).insert(ignore_permissions=True)


def make_item(code, group, **extra):
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": f"{code} name",
			"item_group": group,
			"stock_uom": "Nos",
			"is_stock_item": 0,
			"is_sales_item": 1,
			"include_item_in_manufacturing": 0,
			**extra,
		}
	).insert(ignore_permissions=True)


def fresh_item(root):
	"""A new item without prices in a leaf group of `root`'s subtree."""
	code = f"{P}-ITEM-{frappe.generate_hash(length=6).upper()}"
	make_item(code, leaf_group(root))
	return code


def make_price(item, price_list, rate):
	doc = frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item,
			"price_list": price_list,
			"price_list_rate": rate,
			"uom": "Nos",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def set_rules(doctype, role, rights):
	if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0}):
		add_permission(doctype, role, 0)
	for right in rights:
		update_permission_property(doctype, role, 0, right, 1, validate=False)
	frappe.clear_cache(doctype=doctype)


def set_companies(mapping):
	for name, company in mapping.items():
		frappe.db.set_value("Price List", name, price_lists.FIELD, company, update_modified=False)
	price_lists.clear_caches()


def as_user(user):
	frappe.set_user(user)
	price_lists.clear_caches()


class TestPriceLists(IntegrationTestCase):
	COMPANIES = {L_4S: CO_4S, L_VXM: CO_VXM, L_VXN: CO_VXN, L_USD: CO_4S}

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if not frappe.get_meta("Price List").has_field(price_lists.FIELD):
			# the data step's field (DDL commits: it is created before any fixture of this class)
			create_custom_field(
				"Price List",
				{
					"fieldname": price_lists.FIELD,
					"label": "Company",
					"fieldtype": "Link",
					"options": "Company",
					"insert_after": "currency",
					"in_list_view": 1,
					"in_standard_filter": 1,
				},
			)
			frappe.db.commit()
		if not frappe.get_meta("Company").has_field(price_lists.DEFAULT_FIELD):
			# the release's own field (after_migrate): missing when the tree was copied over without a migrate
			from fours_customizations.install import create_company_custom_fields

			create_company_custom_fields()
			frappe.db.commit()
		super().setUpClass()
		frappe.set_user("Administrator")
		# the site's own lists lose their companies for the class (a site after the split's data step): only the
		# class's lists belong to companies
		frappe.db.sql(f"update `tabPrice List` set `{price_lists.FIELD}` = null")
		price_lists.clear_caches()
		for role in (WRITER_ROLE, READER_ROLE):
			make_role(role)
		set_rules("Item Price", WRITER_ROLE, ("read", "write", "create", "delete"))
		set_rules("Item Price", READER_ROLE, ("read",))
		set_rules("Price List", WRITER_ROLE, ("read", "write", "create"))
		set_rules("Price List", "System Manager", ("read", "write", "create"))
		for name in (L_4S, L_VXM, L_VXN, L_OLD):
			make_list(name)
		make_list(L_BUY, selling=0)
		make_list(L_USD, currency="USD")
		make_item(I_4S, leaf_group("4S Items"))
		make_item(I_VOX, leaf_group("Vox Items"))
		make_item(I_RAW, leaf_group("Raw Kitchen Items"))
		make_item(I_OTHER, leaf_group(outside=("4S Items", "Vox Items", "Raw Kitchen Items")))
		cls.prices = {}
		for item, lists in (
			(I_4S, (L_4S, L_BUY, L_OLD, L_USD)),
			(I_VOX, (L_VXM, L_VXN, L_BUY, L_OLD)),
			(I_RAW, (L_VXN, L_BUY)),
			(I_OTHER, (L_BUY,)),
		):
			for price_list in lists:
				cls.prices[(item, price_list)] = make_price(item, price_list, 1000)
		make_user(U["s4"], [WRITER_ROLE], [CO_4S])
		make_user(U["vxm"], [WRITER_ROLE], [CO_VXM])
		make_user(U["vxn"], [WRITER_ROLE], [CO_VXN])
		make_user(U["vxm_item_rows"], [WRITER_ROLE], [CO_VXM], item_only=[CO_VXN])
		make_user(U["free"], [WRITER_ROLE])
		make_user(U["ml"], [WRITER_ROLE], [CO_ML])
		make_user(U["reader4s"], [READER_ROLE], [CO_4S])
		make_user(U["sm_vxm"], ["System Manager", WRITER_ROLE], [CO_VXM])
		make_user(U["sm"], ["System Manager", WRITER_ROLE])
		make_user(U["ip_vxm"], [WRITER_ROLE], price_only=[CO_VXM])
		set_companies(cls.COMPANIES)
		for company, price_list in ((CO_4S, L_4S), (CO_VXM, L_VXM), (CO_VXN, L_VXN)):
			frappe.db.set_value("Company", company, price_lists.DEFAULT_FIELD, price_list, update_modified=False)
		price_lists.clear_caches()

	def tearDown(self):
		as_user("Administrator")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		super().tearDownClass()
		price_lists.clear_caches()

	# -- helpers ------------------------------------------------------------------------------------------------------

	def visible(self, user):
		"""(item, list) of the class's fixture rows `user` sees through get_list (the permission query)."""
		rows = frappe.get_list(
			"Item Price",
			filters={"name": ["in", list(self.prices.values())]},
			fields=["item_code", "price_list"],
			user=user,
			limit_page_length=0,
		)
		return {(r.item_code, r.price_list) for r in rows}

	def can(self, user, ptype, item, price_list):
		doc = frappe.get_doc("Item Price", self.prices[(item, price_list)])
		return frappe.has_permission("Item Price", ptype, doc=doc, user=user)

	def new_price(self, user, item, price_list, rate=500):
		as_user(user)
		doc = frappe.get_doc(
			{"doctype": "Item Price", "item_code": item, "price_list": price_list, "price_list_rate": rate, "uom": "Nos"}
		)
		doc.insert()
		return doc

	# -- permissions ----------------------------------------------------------------------------------------------------

	def test_read_matrix_per_company(self):
		own = {
			"s4": {(I_4S, L_4S), (I_4S, L_BUY), (I_4S, L_USD), (I_4S, L_OLD)},
			"vxm": {(I_VOX, L_VXM), (I_VOX, L_BUY), (I_RAW, L_BUY), (I_VOX, L_OLD)},
			"vxn": {(I_VOX, L_VXN), (I_RAW, L_VXN), (I_VOX, L_BUY), (I_RAW, L_BUY), (I_VOX, L_OLD)},
			# an Item-only Company row does not count
			"vxm_item_rows": {(I_VOX, L_VXM), (I_VOX, L_BUY), (I_RAW, L_BUY), (I_VOX, L_OLD)},
			"ml": {(I_OTHER, L_BUY)},  # a company outside the families sees only items of no family
			"reader4s": {(I_4S, L_4S), (I_4S, L_BUY), (I_4S, L_USD), (I_4S, L_OLD)},
			"sm_vxm": {(I_VOX, L_VXM), (I_VOX, L_BUY), (I_RAW, L_BUY), (I_VOX, L_OLD)},
		}
		everything = set(self.prices)
		own["ip_vxm"] = own["vxm"]  # a Company row scoped to Item Price restricts prices the same way
		own["free"] = own["sm"] = everything
		for key, expected in own.items():
			with self.subTest(user=key):
				self.assertEqual(self.visible(U[key]), expected)
				for item, price_list in everything:
					self.assertEqual(
						self.can(U[key], "read", item, price_list),
						(item, price_list) in expected,
						(key, item, price_list),
					)
		# Administrator: no condition at all
		self.assertEqual(price_lists.item_price_query_conditions("Administrator"), "")
		self.assertTrue(self.can("Administrator", "read", I_4S, L_4S))

	def test_write_create_delete_per_company(self):
		self.assertTrue(self.can(U["vxm"], "write", I_VOX, L_VXM))
		self.assertFalse(self.can(U["vxm"], "write", I_VOX, L_VXN))
		self.assertFalse(self.can(U["vxm"], "delete", I_4S, L_4S))
		self.assertFalse(self.can(U["s4"], "write", I_VOX, L_BUY))  # a shared list: only the user's family
		self.assertTrue(self.can(U["s4"], "write", I_4S, L_BUY))
		# create on another company's list: denied; on the own list: allowed
		self.assertRaises(frappe.PermissionError, self.new_price, U["vxm"], I_VOX, L_VXN)
		self.assertRaises(frappe.PermissionError, self.new_price, U["vxm"], I_4S, L_4S)
		self.assertRaises(frappe.PermissionError, self.new_price, U["vxm"], I_4S, L_BUY, 1)  # other family
		self.assertTrue(self.new_price(U["vxm"], I_RAW, L_VXM).name)
		self.assertTrue(self.new_price(U["vxm"], I_RAW, L_OLD, 3).name)  # a list of no company, own family
		# delete
		as_user(U["vxn"])
		self.assertRaises(frappe.PermissionError, frappe.delete_doc, "Item Price", self.prices[(I_VOX, L_VXM)])
		as_user("Administrator")
		doc = self.new_price(U["vxn"], fresh_item("Vox Items"), L_VXN, 777)
		frappe.delete_doc("Item Price", doc.name)
		self.assertFalse(frappe.db.exists("Item Price", doc.name))

	def test_write_checks_stored_and_new_list(self):
		# moving an own row into another company's list
		as_user(U["vxm"])
		doc = frappe.get_doc("Item Price", self.prices[(I_VOX, L_VXM)])
		doc.price_list = L_VXN
		self.assertRaises(frappe.PermissionError, doc.save)
		# claiming another company's row by pointing it at the own list (frappe.client.set_value path)
		as_user("Administrator")
		other = frappe.get_doc("Item Price", self.prices[(I_VOX, L_VXN)])
		other.price_list = L_VXM
		self.assertFalse(frappe.has_permission("Item Price", "write", doc=other, user=U["vxm"]))
		self.assertFalse(frappe.has_permission("Item Price", "delete", doc=other, user=U["vxm"]))
		as_user(U["vxm"])
		self.assertRaises(
			frappe.PermissionError,
			frappe.client.set_value,
			"Item Price",
			self.prices[(I_VOX, L_VXN)],
			"price_list",
			L_VXM,
		)
		as_user("Administrator")
		self.assertEqual(frappe.db.get_value("Item Price", self.prices[(I_VOX, L_VXN)], "price_list"), L_VXN)

	def test_disabled_lists_hidden(self):
		frappe.db.set_value("Price List", L_VXM, "enabled", 0, update_modified=False)
		price_lists.clear_caches()
		try:
			self.assertNotIn((I_VOX, L_VXM), self.visible(U["vxm"]))
			self.assertFalse(self.can(U["vxm"], "read", I_VOX, L_VXM))
			self.assertTrue(self.can(U["free"], "read", I_VOX, L_VXM))
			self.assertIn((I_VOX, L_VXM), self.visible(U["free"]))
			# a disabled list without a company (the Standard Selling archive) is hidden as well
			frappe.db.set_value("Price List", L_OLD, "enabled", 0, update_modified=False)
			price_lists.clear_caches()
			self.assertFalse(self.can(U["vxm"], "read", I_VOX, L_OLD))
			self.assertNotIn((I_VOX, L_OLD), self.visible(U["vxm"]))
		finally:
			frappe.db.set_value("Price List", L_VXM, "enabled", 1, update_modified=False)
			frappe.db.set_value("Price List", L_OLD, "enabled", 1, update_modified=False)
			price_lists.clear_caches()

	def test_kit_pos_device_session_keeps_access(self):
		as_user(U["vxm"])
		frappe.local.kit_pos_device = frappe._dict(name="PST-DEVICE", user=U["vxm"])
		try:
			price_lists.clear_caches()
			self.assertIsNone(price_lists.allowed_companies())
			self.assertTrue(self.can(U["vxm"], "read", I_4S, L_4S))
			# a check for another user inside the device request stays restricted
			self.assertFalse(self.can(U["vxn"], "read", I_4S, L_4S))
		finally:
			del frappe.local.kit_pos_device
			price_lists.clear_caches()

	def test_per_user_cache_keeps_publish_cheap(self):
		"""saleslive asks has_permission(read) for every enabled user on each Item Price event: after the first user of
		a request, each further check costs no query."""
		doc = frappe.get_doc("Item Price", self.prices[(I_VOX, L_VXM)])
		users = list(U.values())
		for user in users:
			price_lists.item_price_has_permission(doc, "read", user)
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
			for _ in range(5):
				for user in users:
					price_lists.item_price_has_permission(doc, "read", user)
		self.assertEqual(sql.call_count, 0)
		self.assertIn("saleslive", frappe.get_installed_apps())  # the events above ran through saleslive too

	# -- guards ---------------------------------------------------------------------------------------------------------

	def test_price_list_guard(self):
		for key in ("vxm", "sm_vxm"):
			as_user(U[key])
			doc = frappe.get_doc("Price List", L_VXM)
			doc.enabled = 0
			self.assertRaises(frappe.PermissionError, doc.save)
			doc = frappe.get_doc("Price List", L_OLD)
			doc.currency = "USD"
			self.assertRaises(frappe.PermissionError, doc.save)
			doc = frappe.get_doc("Price List", L_OLD)
			doc.set(price_lists.FIELD, CO_VXM)
			self.assertRaises(frappe.PermissionError, doc.save)
			self.assertRaises(
				frappe.PermissionError, frappe.rename_doc, "Price List", L_OLD, f"{P} Renamed", force=True
			)
		# the title of a list (shown to every company) is guarded too
		as_user(U["vxm"])
		doc = frappe.get_doc("Price List", L_BUY)
		doc.price_list_name = f"{P} Vox Buying"
		self.assertRaisesRegex(frappe.PermissionError, "Price List Name", doc.save)
		self.assertRaises(
			frappe.PermissionError, frappe.client.set_value, "Price List", L_BUY, "price_list_name", f"{P} Vox Buying"
		)
		# a value sent as true/false instead of 1/0 is no change
		as_user(U["vxm"])
		doc = frappe.get_doc("Price List", L_VXM)
		doc.enabled, doc.selling = True, True
		doc.save()
		# an unrestricted System Manager and Administrator may
		for user in (U["sm"], "Administrator"):
			as_user(user)
			doc = frappe.get_doc("Price List", L_OLD)
			doc.enabled = 0
			doc.save()
			doc.enabled = 1
			doc.save()
		# a new selling list needs a company once the split is active
		as_user("Administrator")
		new = frappe.get_doc(
			{"doctype": "Price List", "price_list_name": f"{P} New", "selling": 1, "currency": "UGX", "enabled": 1}
		)
		self.assertRaises(frappe.ValidationError, new.insert)
		new.set(price_lists.FIELD, CO_VXN)
		new.insert()
		# a restricted user creates lists only for their companies; with a Company row scoped to Item Price, Frappe's
		# link check does not apply to Price List: the release's check is the only guard
		as_user(U["ip_vxm"])
		other = frappe.get_doc(
			{"doctype": "Price List", "price_list_name": f"{P} Other", "selling": 1, "currency": "UGX", "enabled": 1}
		)
		other.set(price_lists.FIELD, CO_4S)
		self.assertRaisesRegex(frappe.PermissionError, "may not create a price list for", other.insert)
		other.set(price_lists.FIELD, CO_VXM)
		other.insert()
		self.assertEqual(frappe.db.get_value("Price List", other.name, price_lists.FIELD), CO_VXM)
		# the class's other tests know only the class's lists
		as_user("Administrator")
		for name in (new.name, other.name):
			frappe.delete_doc("Price List", name, force=True)
		price_lists.clear_caches()

	def test_list_in_use_cannot_be_disabled(self):
		profile = frappe.get_all("POS Profile", filters={"disabled": 0}, fields=["name", "selling_price_list"], limit=1)[0]
		location = frappe.get_all(
			"Kit POS Location", filters={"active": 1}, fields=["name", "selling_price_list"], limit=1
		)[0]
		frappe.db.set_value("POS Profile", profile.name, "selling_price_list", L_OLD, update_modified=False)
		try:
			as_user("Administrator")
			doc = frappe.get_doc("Price List", L_OLD)
			doc.enabled = 0
			self.assertRaisesRegex(frappe.ValidationError, "POS Profile", doc.save)
			frappe.db.set_value("POS Profile", profile.name, "selling_price_list", L_VXM, update_modified=False)
			frappe.db.set_value("Kit POS Location", location.name, "selling_price_list", L_OLD, update_modified=False)
			doc = frappe.get_doc("Price List", L_OLD)
			doc.enabled = 0
			self.assertRaisesRegex(frappe.ValidationError, "Kit POS Location", doc.save)
		finally:
			frappe.db.set_value("POS Profile", profile.name, "selling_price_list", profile.selling_price_list)
			frappe.db.set_value("Kit POS Location", location.name, "selling_price_list", location.selling_price_list)

	def test_consumer_must_use_enabled_list_of_its_company(self):
		hooks = frappe.get_hooks("doc_events")
		for doctype in ("POS Profile", "Kit POS Location"):
			self.assertIn("fours_customizations.price_lists.validate_consumer_list", hooks[doctype]["validate"])
		profile = frappe.get_doc("POS Profile", frappe.get_all("POS Profile", filters={"company": CO_VXN}, pluck="name")[0])
		profile.disabled = 0
		profile.selling_price_list = L_VXM
		self.assertRaisesRegex(frappe.ValidationError, "cannot use", price_lists.validate_consumer_list, profile)
		profile.selling_price_list = L_VXN
		price_lists.validate_consumer_list(profile)
		profile.selling_price_list = L_OLD  # a list of no company
		price_lists.validate_consumer_list(profile)
		frappe.db.set_value("Price List", L_OLD, "enabled", 0, update_modified=False)
		price_lists.clear_caches()
		self.assertRaisesRegex(frappe.ValidationError, "disabled", price_lists.validate_consumer_list, profile)
		profile.disabled = 1  # a disabled profile may keep the archive
		price_lists.validate_consumer_list(profile)
		frappe.db.set_value("Price List", L_OLD, "enabled", 1, update_modified=False)
		price_lists.clear_caches()

	def test_item_family_allowlist(self):
		as_user("Administrator")
		for item, price_list in ((I_4S, L_VXM), (I_VOX, L_4S), (I_OTHER, L_4S), (I_OTHER, L_VXN), (I_RAW, L_4S)):
			with self.subTest(item=item, price_list=price_list):
				doc = frappe.get_doc(
					{"doctype": "Item Price", "item_code": item, "price_list": price_list, "price_list_rate": 1, "uom": "Nos"}
				)
				self.assertRaises(price_lists.ItemFamilyError, doc.insert, ignore_permissions=True)
		self.assertTrue(make_price(fresh_item("Raw Kitchen Items"), L_VXM, 10))
		self.assertTrue(make_price(fresh_item("Vox Items"), L_VXN, 10))
		self.assertTrue(make_price(fresh_item("4S Items"), L_4S, 10))
		self.assertTrue(make_price(I_OTHER, L_OLD, 10))  # lists without a company are not guarded
		with patch.dict(frappe.conf, {price_lists.GUARD_CONFIG: 0}):
			self.assertTrue(make_price(I_OTHER, L_4S, 10))

	def test_party_default_list(self):
		as_user("Administrator")
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"{CUSTOMER} Party",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}),
				"custom_company": CO_VXM,
			}
		).insert(ignore_permissions=True)
		as_user(U["vxm"])
		for value in (L_VXN, L_4S, L_OLD):  # another company's list, or a list of no company (it would redirect a till)
			customer.default_price_list = value
			self.assertRaises(frappe.PermissionError, price_lists.validate_party_default_list, customer)
		for value in (L_VXM, None):
			customer.default_price_list = value
			price_lists.validate_party_default_list(customer)
		supplier = frappe.new_doc("Supplier")
		supplier.default_price_list = L_BUY  # suppliers: the shared buying list is fine
		price_lists.validate_party_default_list(supplier)
		supplier.default_price_list = L_VXN
		self.assertRaises(frappe.PermissionError, price_lists.validate_party_default_list, supplier)
		# a disabled list is never a new default, also not for an unrestricted user
		frappe.db.set_value("Price List", L_OLD, "enabled", 0, update_modified=False)
		price_lists.clear_caches()
		as_user(U["free"])
		customer.default_price_list = L_OLD
		self.assertRaisesRegex(frappe.ValidationError, "disabled", price_lists.validate_party_default_list, customer)
		frappe.db.set_value("Price List", L_OLD, "enabled", 1, update_modified=False)
		price_lists.clear_caches()
		as_user(U["free"])
		customer.default_price_list = L_VXN
		price_lists.validate_party_default_list(customer)
		# an unchanged value is not re-judged
		as_user("Administrator")
		customer.default_price_list = L_VXN
		customer.save(ignore_permissions=True)
		as_user(U["vxm"])
		customer.reload()
		customer._doc_before_save = frappe.get_doc("Customer", customer.name)
		price_lists.validate_party_default_list(customer)
		hooks = frappe.get_hooks("doc_events")
		for doctype in ("Customer", "Customer Group", "Supplier", "Supplier Group"):
			self.assertIn("fours_customizations.price_lists.validate_party_default_list", hooks[doctype]["validate"])

	# -- documents and items --------------------------------------------------------------------------------------------

	def test_selling_list_for(self):
		self.assertEqual(price_lists.selling_list_for(L_OLD, CO_VXM), L_OLD)
		frappe.db.set_value("Price List", L_OLD, "enabled", 0, update_modified=False)
		price_lists.clear_caches()
		try:
			self.assertEqual(price_lists.selling_list_for(L_OLD, CO_VXM), L_VXM)
			self.assertEqual(price_lists.selling_list_for(L_OLD, CO_VXN, "UGX"), L_VXN)
			self.assertEqual(price_lists.selling_list_for(L_OLD, CO_VXN, "USD"), L_OLD)  # never a currency change
			self.assertEqual(price_lists.selling_list_for(L_OLD, CO_ML), L_OLD)  # no default list: unchanged
			as_user(U["vxm"])
			self.assertEqual(price_lists.selling_list_replacement(CO_VXM, L_OLD), L_VXM)
			self.assertIsNone(price_lists.selling_list_replacement(CO_VXN, L_OLD))  # not the user's company
			self.assertIsNone(price_lists.selling_list_replacement(CO_VXM, L_VXM))
		finally:
			as_user("Administrator")
			frappe.db.set_value("Price List", L_OLD, "enabled", 1, update_modified=False)
			price_lists.clear_caches()

	def make_invoice(self, rate, item=I_VOX, submit=True):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": CO_VXM,
				"customer": self.customer(),
				"selling_price_list": L_OLD,
				"posting_date": nowdate(),
				"due_date": nowdate(),
				"items": [{"item_code": item, "qty": 2, "rate": rate, "price_list_rate": rate}],
			}
		)
		doc.insert(ignore_permissions=True)
		if submit:
			doc.submit()
		return doc

	def customer(self):
		if not frappe.db.exists("Customer", CUSTOMER):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": CUSTOMER,
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}),
					"territory": frappe.db.get_value("Territory", {"is_group": 0}),
					"custom_company": CO_VXM,
				}
			).insert(ignore_permissions=True)
		return CUSTOMER

	def test_presplit_documents_with_archive_disabled(self):
		"""Amendment, return and mapped document of documents made on the (now disabled) old list save, submit and print
		on the company's list, keep their line rates, and auto-insert a missing price only into the enabled list."""
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
		from erpnext.selling.doctype.quotation.quotation import make_sales_order

		as_user("Administrator")
		frappe.db.set_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing", 1)
		frappe.db.set_single_value("Stock Settings", "update_existing_price_list_rate", 0)
		original = self.make_invoice(1234)
		returned_source = self.make_invoice(1500)
		quoted = fresh_item("Raw Kitchen Items")
		quotation = frappe.get_doc(
			{
				"doctype": "Quotation",
				"company": CO_VXM,
				"quotation_to": "Customer",
				"party_name": self.customer(),
				"selling_price_list": L_OLD,
				"transaction_date": nowdate(),
				"valid_till": add_days(nowdate(), 30),
				"items": [{"item_code": quoted, "qty": 1, "rate": 2222, "price_list_rate": 2222}],
			}
		).insert(ignore_permissions=True)
		quotation.submit()
		# the quoted item has no price on the old list (ERPNext's auto-insert made one while it was enabled: gone)
		frappe.db.delete("Item Price", {"item_code": quoted, "price_list": L_OLD})
		frappe.db.set_value("Price List", L_OLD, "enabled", 0, update_modified=False)
		frappe.cache.hdel("price_list_details", L_OLD)
		price_lists.clear_caches()

		original.reload()
		original.cancel()
		amended = frappe.copy_doc(original)
		amended.amended_from = original.name
		amended.docstatus = 0
		amended.insert(ignore_permissions=True)
		amended.submit()
		self.assertEqual(amended.selling_price_list, L_VXM)
		self.assertEqual(flt(amended.items[0].rate), 1234)  # not re-priced from the Makindye list (1000)
		self.assertIn(amended.name, frappe.get_print("Sales Invoice", amended.name, print_format="Standard"))

		credit = make_sales_return(returned_source.name)
		credit.flags.allow_manual_goods_return = True  # fours routes goods returns through the store; not tested here
		credit.insert(ignore_permissions=True)
		credit.submit()
		self.assertEqual(credit.selling_price_list, L_VXM)
		self.assertEqual(flt(credit.items[0].rate), 1500)

		order = make_sales_order(quotation.name)  # priced on the server on the disabled list: no insert there
		order.delivery_date = add_days(nowdate(), 1)
		order.set_warehouse = frappe.db.get_value("Warehouse", {"company": CO_VXM, "is_group": 0})
		order.insert(ignore_permissions=True)
		self.assertEqual(order.selling_price_list, L_VXM)
		self.assertEqual(flt(order.items[0].rate), 2222)
		# the quoted item had no Makindye price: the auto-insert went to the enabled list, never to the archive
		self.assertTrue(frappe.db.exists("Item Price", {"item_code": quoted, "price_list": L_VXM}))
		self.assertEqual(frappe.db.count("Item Price", {"item_code": quoted, "price_list": L_OLD}), 0)

		# a Vox price writer (company restricted) amends a pre-split invoice of an item without a Makindye price: the
		# auto-insert passes the Item Price hook because it targets the enabled list (the archive would be refused)
		set_rules("Sales Invoice", WRITER_ROLE, ("read", "write", "create", "submit", "cancel", "amend"))
		for doctype in ("Customer", "Item", "Company", "Price List"):
			set_rules(doctype, WRITER_ROLE, ("read",))
		frappe.db.set_value("Price List", L_OLD, "enabled", 1, update_modified=False)
		frappe.cache.hdel("price_list_details", L_OLD)
		price_lists.clear_caches()
		second = self.make_invoice(999, item=I_OTHER)
		second.cancel()
		frappe.db.set_value("Price List", L_OLD, "enabled", 0, update_modified=False)
		frappe.cache.hdel("price_list_details", L_OLD)
		unpriced = fresh_item("Vox Items")
		price_lists.clear_caches()
		self.assertFalse(price_lists.row_visible(U["vxm"], L_OLD, unpriced))
		as_user(U["vxm"])
		amended = frappe.copy_doc(second)
		amended.amended_from = second.name
		amended.docstatus = 0
		amended.items[0].item_code = unpriced
		amended.insert()
		self.assertEqual(amended.selling_price_list, L_VXM)
		self.assertEqual(flt(amended.items[0].rate), 999)
		self.assertTrue(frappe.db.exists("Item Price", {"item_code": unpriced, "price_list": L_VXM, "owner": U["vxm"]}))
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": unpriced, "price_list": L_OLD}))
		as_user("Administrator")
		frappe.db.set_value("Price List", L_OLD, "enabled", 1, update_modified=False)
		frappe.cache.hdel("price_list_details", L_OLD)
		price_lists.clear_caches()

	def test_new_item_defaults_get_company_list(self):
		as_user("Administrator")
		make_item(
			f"{P}-QA-1",
			leaf_group("Vox Items"),
			standard_rate=4500,
			item_defaults=[{"company": CO_VXM}, {"company": CO_VXN, "default_price_list": L_OLD}],
		)
		item = frappe.get_doc("Item", f"{P}-QA-1")
		lists = {row.company: row.default_price_list for row in item.item_defaults}
		self.assertEqual(lists, {CO_VXM: L_VXM, CO_VXN: L_OLD})
		# ERPNext's Standard Selling Rate prices went to the Item Default lists only (not to Selling Settings' list)
		self.assertEqual(
			set(frappe.get_all("Item Price", filters={"item_code": item.name}, pluck="price_list")), {L_VXM, L_OLD}
		)
		# an existing item is left alone
		item.append("item_defaults", {"company": CO_4S})
		item.save()
		self.assertFalse(item.item_defaults[-1].default_price_list)

	def test_kit_pos_quick_add_writes_only_the_branch_list(self):
		"""kit_pos catalog.create_product (the real endpoint; only the request, the till's access and its location are
		stubbed) prices the location's list plus each Item Default's list (else the Selling Settings list): with the
		release's Item Default fill, only the branch list."""
		catalog = frappe.get_module("kit_pos.connector.catalog")
		as_user("Administrator")
		frappe.db.set_single_value("Selling Settings", "selling_price_list", L_OLD)  # without the fill: priced here too
		warehouse = frappe.db.get_value("Warehouse", {"company": CO_VXM, "is_group": 0, "disabled": 0})
		location = frappe._dict(name="PST-LOC", company=CO_VXM, selling_price_list=L_VXM, warehouse=warehouse)
		code = f"{P}-QA-2-{frappe.generate_hash(length=4).upper()}"
		body = {
			"location_id": "PST-LOC",
			"name": f"{code} name",
			"sku": code,
			"single_dsp": 4500,
			"category": leaf_group("Vox Items"),
			"unit_name": "Nos",
		}
		access = SimpleNamespace(require=lambda *a, **k: None, require_location=lambda location_id: location_id)
		with (
			patch.object(catalog, "request_json", return_value=body),
			patch.object(catalog.sale_permissions, "request_access", return_value=access),
			patch.object(catalog, "_selected_location", return_value=location),
		):
			result = catalog.create_product()
		self.assertTrue(result["id"])
		rows = frappe.get_all("Item Price", filters={"item_code": code}, fields=["price_list", "price_list_rate"])
		self.assertEqual({(r.price_list, flt(r.price_list_rate)) for r in rows}, {(L_VXM, 4500)})
		item = frappe.get_doc("Item", code)
		self.assertEqual([(d.company, d.default_price_list) for d in item.item_defaults], [(CO_VXM, L_VXM)])

	def test_landed_cost_scope(self):
		as_user("Administrator")
		voucher = SimpleNamespace(
			name="PST-LCV",
			company=CO_VXM,
			items=[SimpleNamespace(item_code=I_VOX, new_selling_price=4321), SimpleNamespace(item_code=I_RAW, new_selling_price=99)],
		)
		rate = lambda item, pl: flt(frappe.db.get_value("Item Price", {"item_code": item, "price_list": pl}, "price_list_rate"))
		before = {(i, pl): rate(i, pl) for i, pl in ((I_VOX, L_VXN), (I_VOX, L_OLD), (I_4S, L_USD), (I_4S, L_OLD))}
		missing = fresh_item("Raw Kitchen Items")
		voucher.items[1].item_code = missing
		landed_cost_handler.on_submit(voucher)
		self.assertEqual(rate(I_VOX, L_VXM), 4321)
		self.assertEqual(rate(I_VOX, L_VXN), before[(I_VOX, L_VXN)])  # another company's list
		self.assertEqual(rate(I_VOX, L_OLD), before[(I_VOX, L_OLD)])  # a list of no company
		self.assertEqual(rate(missing, L_VXM), 99)  # missing: created in the company's default list
		self.assertEqual(frappe.db.count("Item Price", {"item_code": missing}), 1)
		# a 4S voucher never touches the 4S USD list
		landed_cost_handler.on_submit(
			SimpleNamespace(name="PST-LCV-2", company=CO_4S, items=[SimpleNamespace(item_code=I_4S, new_selling_price=7)])
		)
		self.assertEqual(rate(I_4S, L_4S), 7)
		self.assertEqual(rate(I_4S, L_USD), before[(I_4S, L_USD)])
		self.assertEqual(rate(I_4S, L_OLD), before[(I_4S, L_OLD)])

	# -- Quick Price page -----------------------------------------------------------------------------------------------

	def test_quick_price_lists_per_company(self):
		# Nansana invoices on the Makindye list (history): the list is still Makindye's only, never "shared"
		name = frappe.generate_hash(length=12)
		frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"name": name,
				"company": CO_VXN,
				"selling_price_list": L_VXM,
				"posting_date": nowdate(),
				"docstatus": 1,
			}
		).db_insert()
		api.clear_cache()
		usage = api.list_usage()
		self.assertIn(L_VXM, usage[CO_VXN])
		self.assertEqual(api.company_lists(CO_VXM, usage), [L_VXM])
		self.assertEqual(api.company_lists(CO_4S, usage)[0], L_4S)
		self.assertIn(L_USD, api.company_lists(CO_4S, usage))
		self.assertNotIn(
			frappe.db.get_single_value("Selling Settings", "selling_price_list"), api.company_lists(CO_VXN, usage)
		)
		self.assertEqual(api.users_of(L_VXM, usage), [CO_VXM])
		self.assertIsNone(api.shared_notice(L_VXM, api.users_of(L_VXM, usage), CO_VXM))
		self.assertEqual(api.accessible_companies(U["vxm_item_rows"]), [CO_VXM])
		self.assertEqual(api.accessible_companies(U["s4"]), [CO_4S])
		names = {row.name for row in api.selectable_lists(U["vxm"])}
		self.assertIn(L_VXM, names)
		self.assertIn(L_OLD, names)
		self.assertFalse(names & {L_VXN, L_4S, L_USD})
		self.assertTrue({L_VXN, L_4S} <= {row.name for row in api.selectable_lists(U["free"])})

	def test_quick_price_family_message_and_hidden_cells(self):
		as_user(U["vxm"])
		self.assertFalse(api.cell_visible(I_4S, L_OLD))
		self.assertTrue(api.cell_visible(I_VOX, L_OLD))
		self.assertTrue(api.family_refused(I_4S, L_VXM))
		access = api.cell_access(I_4S, L_VXM, {"record_uom": "Nos"}, None, True)
		self.assertEqual(access["editable"], 0)
		self.assertIn("cannot be priced", access["lock_reason"])
		ctx = frappe._dict(company=CO_VXM, lists=[L_VXM], used_by={L_VXM: [CO_VXM]})
		change = SimpleNamespace(item_code=I_4S, price_list=L_VXM, uom="Nos", new_rate=10, expected_old_rate=0)
		self.assertRaisesRegex(price_lists.ItemFamilyError, "takes only items of", api.save_one, ctx, change, "", 0)

	def test_quick_price_page_end_to_end(self):
		from fours_customizations.quick_price import setup

		setup.sync_access(verbose=False)
		api.clear_caches()
		as_user(U["vxm_item_rows"])
		context = api.get_context()
		self.assertEqual(context["companies"], [CO_VXM])
		self.assertFalse({row.name for row in context["selling_lists"]} & {L_VXN, L_4S})
		self.assertRaises(frappe.PermissionError, api.get_lists, CO_VXN)
		lists = api.get_lists(CO_VXM)["lists"]
		self.assertEqual([row["price_list"] for row in lists], [L_VXM])
		self.assertIsNone(lists[0]["notice"])
		page = api.get_items(CO_VXM, lists=[L_OLD, L_VXN], search=P, all_sales_items=1)
		self.assertEqual([row["price_list"] for row in page["lists"]], [L_VXM, L_OLD])  # never another company's
		items = {row["item_code"]: row for row in page["items"]}
		self.assertEqual(items[I_4S]["prices"][L_OLD].get("hidden"), 1)  # a 4S price on a list of no company
		self.assertIsNone(items[I_4S]["prices"][L_OLD]["rate"])
		self.assertEqual(items[I_4S]["prices"][L_VXM]["editable"], 0)  # outside the Makindye list's family
		cell = items[I_VOX]["prices"][L_VXM]
		self.assertEqual(cell["editable"], 1)
		change = {
			"item_code": I_VOX,
			"price_list": L_VXM,
			"uom": "Nos",
			"item_price": cell["item_price"],
			"expected_old_rate": cell["rate"],
			"new_rate": flt(cell["rate"]) + 10,
		}
		result = api.save_prices(CO_VXM, [change, dict(change, price_list=L_VXN)])
		self.assertEqual([r["ok"] for r in result], [1, 0])
		log = frappe.get_doc("Quick Price Change", result[0]["log"])
		self.assertFalse(log.shared_with)
		self.assertEqual(flt(frappe.db.get_value("Item Price", cell["item_price"], "price_list_rate")), flt(cell["rate"]) + 10)

	def test_automatic_price_writes_skip_archives_and_other_families(self):
		from erpnext.stock import get_item_details
		from erpnext.stock.doctype.item.item import Item

		self.assertTrue(getattr(get_item_details.insert_item_price, "fours_price_lists", False))
		self.assertTrue(getattr(Item.add_price, "fours_price_lists", False))
		as_user("Administrator")
		frappe.db.set_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing", 1)
		# a Makindye sale of a 4S item without a Makindye price saves; no 4S item enters the bar list
		invoice = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": CO_VXM,
				"customer": self.customer(),
				"selling_price_list": L_VXM,
				"posting_date": nowdate(),
				"due_date": nowdate(),
				"items": [{"item_code": I_4S, "qty": 1, "rate": 5, "price_list_rate": 5}],
			}
		).insert(ignore_permissions=True)
		self.assertTrue(invoice.name)
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": I_4S, "price_list": L_VXM}))
		# a new 4S-family item with a Standard Selling Rate and a Makindye Item Default: created, no bar price
		code = f"{P}-QA-4S"
		make_item(code, leaf_group("4S Items"), standard_rate=10, item_defaults=[{"company": CO_VXM}])
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": code}))
		# landed cost: a missing price of another family is not created in the company's list
		landed_cost_handler.on_submit(
			SimpleNamespace(name="PST-LCV-F", company=CO_VXM, items=[SimpleNamespace(item_code=I_4S, new_selling_price=9)])
		)
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": I_4S, "price_list": L_VXM}))
		self.assertTrue(price_lists.automatic_price_refused(L_VXM, I_4S))
		self.assertFalse(price_lists.automatic_price_refused(L_VXM, I_VOX))
		with patch.dict(frappe.conf, {price_lists.GUARD_CONFIG: 0}):
			self.assertFalse(price_lists.automatic_price_refused(L_VXM, I_4S))

	def test_restricted_writer_purchase_skips_rows_they_may_not_use(self):
		"""A company-restricted price writer saves a Purchase Invoice on a list of no company (Standard Buying) for items
		without a price there: ERPNext's auto-insert (as the session user) of an item outside the user's families is
		skipped instead of failing the invoice; an item of the user's family is still priced."""
		as_user("Administrator")
		frappe.db.set_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing", 1)
		frappe.db.set_single_value("Stock Settings", "update_existing_price_list_rate", 0)
		supplier = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": f"{P} Supplier {frappe.generate_hash(length=4)}",
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}),
				"custom_company": CO_VXM,
			}
		).insert(ignore_permissions=True)
		no_family = f"{P}-ITEM-{frappe.generate_hash(length=6).upper()}"
		make_item(no_family, leaf_group(outside=("4S Items", "Vox Items", "Raw Kitchen Items")))
		item_4s, item_vox = fresh_item("4S Items"), fresh_item("Vox Items")
		price_lists.clear_caches()
		as_user(U["vxm"])
		self.assertTrue(frappe.has_permission("Item Price", "write"))  # ERPNext's own (doctype-level) check passes
		self.assertFalse(price_lists.row_visible(U["vxm"], L_BUY, no_family))
		self.assertFalse(price_lists.row_visible(U["vxm"], L_BUY, item_4s))
		invoice = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": CO_VXM,
				"supplier": supplier.name,
				"buying_price_list": L_BUY,
				"posting_date": nowdate(),
				"bill_date": nowdate(),
				"items": [
					{"item_code": code, "qty": 1, "rate": 777, "price_list_rate": 777}
					for code in (no_family, item_4s, item_vox)
				],
			}
		)
		invoice.insert(ignore_permissions=True)  # the invoice's own rights are not the point: the Item Price insert's are
		self.assertTrue(invoice.name)
		as_user("Administrator")
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": no_family}))
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": item_4s}))
		self.assertEqual(
			flt(frappe.db.get_value("Item Price", {"item_code": item_vox, "price_list": L_BUY}, "price_list_rate")), 777
		)
		# unrestricted writers (Administrator, users without a Company row) still auto-insert, as on fe153b5
		self.assertFalse(price_lists.automatic_price_refused(L_BUY, no_family, "Administrator"))
		self.assertFalse(price_lists.automatic_price_refused(L_BUY, no_family, U["free"]))
		self.assertTrue(price_lists.automatic_price_refused(L_BUY, no_family, U["vxm"]))

	def test_patch_erpnext_checks_signatures(self):
		from erpnext.stock import get_item_details
		from erpnext.stock.doctype.item.item import Item

		wrapped_insert, wrapped_add = get_item_details.insert_item_price, Item.add_price
		original_insert, original_add = wrapped_insert.__wrapped__, wrapped_add.__wrapped__
		self.assertTrue(price_lists.signatures_match(original_insert, original_add))  # this ERPNext = multax's

		def changed(ctx, extra=None):
			return original_insert(ctx)

		try:
			get_item_details.insert_item_price, Item.add_price = changed, original_add
			with patch.object(price_lists, "_signature_warned", []), patch.object(frappe, "logger") as logger:
				price_lists.patch_erpnext()
				price_lists.patch_erpnext()
			# a changed signature: ERPNext left unwrapped (both functions), one warning per process
			self.assertIs(get_item_details.insert_item_price, changed)
			self.assertIs(Item.add_price, original_add)
			self.assertEqual(logger.return_value.warning.call_count, 1)
			# the expected signatures: both wrapped, once (a second call is a no-op)
			get_item_details.insert_item_price = original_insert
			price_lists.patch_erpnext()
			first = get_item_details.insert_item_price
			price_lists.patch_erpnext()
			self.assertIs(get_item_details.insert_item_price, first)
			self.assertTrue(getattr(first, "fours_price_lists", False))
			self.assertIs(first.__wrapped__, original_insert)
			self.assertTrue(getattr(Item.add_price, "fours_price_lists", False))
		finally:
			get_item_details.insert_item_price, Item.add_price = wrapped_insert, wrapped_add

	def test_quick_price_log_follows_the_company_rule(self):
		from fours_customizations.fours_customizations.doctype.quick_price_change import quick_price_change

		as_user("Administrator")
		names = {}
		for company, price_list, item in ((CO_4S, L_4S, I_4S), (CO_VXM, L_VXM, I_VOX), (CO_VXM, L_VXN, I_VOX)):
			doc = frappe.get_doc(
				{
					"doctype": "Quick Price Change",
					"company": company,
					"item_code": item,
					"price_list": price_list,
					"uom": "Nos",
					"action": "Changed",
					"old_rate": 1000,
					"new_rate": 1100,
					"user": "Administrator",
					"timestamp": frappe.utils.now_datetime(),
				}
			)
			doc.flags.from_quick_price_update = True
			doc.insert(ignore_permissions=True)
			names[(company, price_list)] = doc.name
		every = set(names.values())
		for key, expected in (
			("ip_vxm", {names[(CO_VXM, L_VXM)]}),  # a Company row scoped to Item Price: no Frappe link filter applies
			("vxm", {names[(CO_VXM, L_VXM)]}),  # not the Makindye row on the Nansana list
			("s4", {names[(CO_4S, L_4S)]}),
			("free", every),
		):
			with self.subTest(user=key):
				got = frappe.get_list("Quick Price Change", filters={"name": ["in", list(every)]}, user=U[key], pluck="name")
				self.assertEqual(set(got), expected)
				for name in every:
					doc = frappe.get_doc("Quick Price Change", name)
					self.assertEqual(bool(quick_price_change.has_permission(doc, "read", U[key])), name in expected)
		# dark mode: no company rule
		set_companies({name: None for name in self.COMPANIES})
		try:
			self.assertEqual(quick_price_change.query_conditions(U["ip_vxm"]), "")
		finally:
			set_companies(self.COMPANIES)

	# -- dark mode ------------------------------------------------------------------------------------------------------

	def test_dark_mode_without_companies(self):
		"""No company on any list: every rule is off and the page lists are fe153b5's."""
		set_companies({name: None for name in self.COMPANIES})
		try:
			self.assertFalse(price_lists.split_active(), price_lists.lists())
			self.assertEqual(price_lists.item_price_query_conditions(U["vxm"]), "")
			self.assertTrue(self.can(U["vxm"], "write", I_4S, L_4S))
			self.assertEqual(self.visible(U["vxm"]), set(self.prices))
			frappe.db.set_value("Price List", L_OLD, "enabled", 0, update_modified=False)
			price_lists.clear_caches()
			self.assertEqual(price_lists.selling_list_for(L_OLD, CO_VXM), L_OLD)
			frappe.db.set_value("Price List", L_OLD, "enabled", 1, update_modified=False)
			price_lists.clear_caches()
			as_user(U["vxm"])
			doc = frappe.get_doc("Price List", L_OLD)
			doc.currency = "UGX"
			price_lists.validate_price_list(doc)  # no guard
			price_lists.before_rename_price_list(doc)
			doc = frappe.get_doc(
				{"doctype": "Item Price", "item_code": I_4S, "price_list": L_VXM, "price_list_rate": 1, "uom": "Nos"}
			)
			price_lists.validate_item_price(doc)  # no family guard
			self.assertFalse(price_lists.automatic_price_refused(L_VXM, I_4S))
			item = frappe.new_doc("Item")
			item.append("item_defaults", {"company": CO_VXM})
			price_lists.fill_item_default_price_lists(item)
			self.assertFalse(item.item_defaults[0].default_price_list)
			as_user("Administrator")
			usage = api.list_usage()
			default = frappe.db.get_single_value("Selling Settings", "selling_price_list")
			self.assertIn(default, api.company_lists(CO_VXM, usage))
			self.assertEqual(
				api.accessible_companies(U["vxm_item_rows"]), sorted([CO_VXM, CO_VXN])
			)  # fe153b5: every Company row counts
			self.assertNotEqual(api.users_of(L_VXM, usage), [CO_VXM])
			voucher = SimpleNamespace(name="PST-LCV-D", company=CO_VXM, items=[SimpleNamespace(item_code=I_VOX, new_selling_price=55)])
			landed_cost_handler.on_submit(voucher)
			rates = frappe.get_all("Item Price", filters={"item_code": I_VOX, "selling": 1}, pluck="price_list_rate")
			self.assertEqual(set(map(flt, rates)), {55})  # fe153b5: every selling price of the item
		finally:
			as_user("Administrator")
			set_companies(self.COMPANIES)

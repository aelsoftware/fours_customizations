"""Isolated modifier tests: in-memory records, no initialized Frappe site."""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fours_customizations.vox_work_orders import _invoice_warehouse_header_matches


class ApiError(Exception):
	def __init__(self, status, body):
		self.status, self.body = status, body
		super().__init__(body["error"]["message"])


class ValidationError(Exception):
	pass


class Row(dict):
	def __getattr__(self, name):
		return self.get(name)

	def __setattr__(self, name, value):
		self[name] = value

	def as_dict(self):
		return deepcopy(dict(self))


class Document(Row):
	def __init__(self, fixture, values):
		super().__init__(deepcopy(values))
		object.__setattr__(self, "fixture", fixture)

	def is_new(self):
		return bool(self.get("__islocal"))

	def insert(self, *, ignore_permissions=False, set_name=None):
		if set_name:
			self.name = set_name
		if self.doctype == self.fixture.provider.SELECTION:
			# Native _validate_links runs before before_insert and replaces the
			# varchar Link with the linked autoincrement document's integer name.
			linked = self.fixture.records[("Kit POS Sale", str(self.kit_pos_sale))]
			self.kit_pos_sale = linked["name"]
		if self.fixture.before_insert:
			self.fixture.before_insert(self)
		self.fixture.provider.validate_private_record(self)
		key = (self.doctype, self.name)
		if key in self.fixture.records:
			raise AssertionError("Duplicate fixture record")
		if self.doctype == self.fixture.provider.OPTION and any(
			kind == self.doctype and row.get("public_id") == self.public_id
			for (kind, _name), row in self.fixture.records.items()
		):
			raise AssertionError("Duplicate public ID")
		self["__islocal"] = 0
		self.fixture.records[key] = self.as_dict()
		self.fixture.writes.append(key)
		return self


class Database:
	def __init__(self, fixture):
		self.fixture = fixture
		self.transaction_writes = 0

	def matching(self, doctype, filters):
		return [
			row
			for (kind, _name), row in self.fixture.records.items()
			if kind == doctype and all(row.get(field) == raw for field, raw in filters.items())
		]

	def exists(self, doctype, name):
		if doctype == "DocType":
			return name in self.fixture.schema
		if isinstance(name, dict):
			return bool(self.matching(doctype, name))
		if doctype == "Kit POS Sale":
			name = str(name)
		return (doctype, name) in self.fixture.records

	def get_value(self, doctype, name, field="name", *, as_dict=False, **kwargs):
		if doctype == "Kit POS Sale" and not isinstance(name, dict):
			name = str(name)
		rows = (
			self.matching(doctype, name)
			if isinstance(name, dict)
			else [self.fixture.records.get((doctype, name))]
		)
		row = next((row for row in rows if row), None)
		if row is None:
			return None
		if isinstance(field, list | tuple):
			return (
				Row({key: row.get(key) for key in field}) if as_dict else tuple(row.get(key) for key in field)
			)
		return row.get(field)

	@staticmethod
	def escape(raw):
		return "'" + raw.replace("'", "''") + "'"


class TestVoxModifiers(TestCase):
	def setUp(self):
		self.records, self.writes = {}, []
		self.before_insert = None
		self.invoice_context = None
		self.schema = {"Vox Kitchen Modifier Option", "Vox Kitchen Modifier Selection", "Kit POS Sale"}
		self.company = "Vox Lounge Nansana"
		self.frappe = ModuleType("frappe")
		self.frappe._dict = Row
		self.frappe.db = Database(self)
		self.frappe.conf = {"vox_kitchen_modifiers": {self.company: 1}}
		self.frappe.session = SimpleNamespace(user="Administrator")
		self.frappe.local = SimpleNamespace(site="isolated.test")
		self.frappe.PermissionError = PermissionError
		self.frappe.get_doc = self.get_doc
		self.frappe.get_all = self.get_all
		self.frappe.throw = lambda message, exc=ValidationError: self.raise_error(exc, message)
		self.frappe.get_roles = lambda user: []
		self.frappe.has_permission = lambda *args, **kwargs: False
		self.location = Row(
			name="Mini Bar",
			company=self.company,
			warehouse="Main Bar - VXN",
			selling_price_list="Selling Price Vox Nansana",
			pos_profile="Mini Bar",
		)
		self.locations = {
			100: self.location,
			200: Row(self.location, name="Other Bar", pos_profile="Other Bar"),
		}
		self.identities = {
			17: "VXN-DRY-GOAT", 18: "VXN-PAN-FRIED-GOAT", 19: "VXN-CHICKEN", 117: "VXN-DRY-GOAT"
		}
		ids = ModuleType("kit_pos.connector.ids")
		ids.lookup = (
			lambda doctype, identity: self.identities.get(int(identity or 0)) if doctype == "Item" else None
		)
		connector = ModuleType("kit_pos.connector")
		connector.__path__ = []
		connector.ids = ids
		kit = ModuleType("kit_pos")
		kit.__path__ = []
		materialize = ModuleType("kit_pos.connector.materialize")
		materialize._location_for = lambda sale: self.locations.get(sale.get("location_id"))
		materialize._pos_profile_for = lambda location: location.get("pos_profile") or location.get("name")
		materialize._invoice_submission_context = lambda invoice: self.invoice_context
		work_orders = ModuleType("fours_customizations.vox_work_orders")
		work_orders._invoice_warehouse_header_matches = _invoice_warehouse_header_matches
		uoms = ModuleType("kit_pos.connector.item_uom")
		uoms.selected_uom = self.selected_uom
		router = ModuleType("kit_pos.connector.router")
		router.ApiError = ApiError
		path = Path(__file__).resolve().parents[1] / "vox_modifiers.py"
		name = "fours_customizations._isolated_vox_modifier_provider"
		spec = importlib.util.spec_from_file_location(name, path)
		self.provider = importlib.util.module_from_spec(spec)
		self.enterContext(
			patch.dict(
				sys.modules,
				{
					"frappe": self.frappe,
					"kit_pos": kit,
					"kit_pos.connector": connector,
					"kit_pos.connector.ids": ids,
					"kit_pos.connector.materialize": materialize,
					"kit_pos.connector.item_uom": uoms,
					"kit_pos.connector.router": router,
					"fours_customizations.vox_work_orders": work_orders,
					name: self.provider,
				},
			)
		)
		spec.loader.exec_module(self.provider)
		self.seed("Company", self.company, default_currency="UGX")
		self.seed("Warehouse", self.location.warehouse, company=self.company, is_group=0)
		for code in self.provider.MENU:
			self.seed(
				"Item",
				code,
				stock_uom="Pc(s)",
				is_sales_item=1,
				is_stock_item=0,
				disabled=1,
				has_variants=0,
				item_defaults=[Row(company=self.company)],
			)
		self.sale = self.seed(
			"Kit POS Sale",
			"1",
			sale_uuid="sale-uuid-1",
			location_id=100,
			pos_opening_entry="OPEN-1",
			materialize_state="draft",
			lines=[],
			sales_invoice="",
		)
		self.line = Row(
			name="LINE-1",
			parent=self.sale.name,
			line_id=101,
			desktop_line_uuid="line-uuid-1",
			product_id=17,
			variation_id=17,
			item_code="VXN-DRY-GOAT",
			sub_unit_id=0,
			quantity=2,
			unit_price_inc_tax=15000,
			line_payload="{}",
		)

	@staticmethod
	def raise_error(exc, message):
		raise exc(message)

	@staticmethod
	def selected_uom(code, stock_uom, subunit, *, client_multiplier=None, client_unit_name=None):
		unit, factor = ("Box", 12) if subunit == 12 else (stock_uom, 1)
		if subunit not in (0, 12):
			raise ValueError("Unknown UOM")
		if client_multiplier is not None and client_multiplier != factor:
			raise ValueError("Wrong multiplier")
		if client_unit_name is not None and client_unit_name != unit:
			raise ValueError("Wrong named UOM")
		return unit, factor

	def seed(self, doctype, name, **values):
		key = str(name) if doctype == "Kit POS Sale" else name
		self.records[(doctype, key)] = {"doctype": doctype, "name": name, **deepcopy(values)}
		return self.get_doc(doctype, name)

	def get_doc(self, doctype, name=None, **kwargs):
		if isinstance(doctype, dict):
			return Document(self, {**doctype, "__islocal": 1})
		if doctype == "Kit POS Sale":
			name = str(name)
		return Document(self, self.records[(doctype, name)])

	def get_all(self, doctype, *, filters=None, fields=None, pluck=None, limit_page_length=0, **kwargs):
		rows = self.frappe.db.matching(doctype, filters or {})
		if limit_page_length:
			rows = rows[:limit_page_length]
		if pluck:
			return [row.get(pluck) for row in rows]
		return [Row({key: row.get(key) for key in fields or ("name",)}) for row in rows]

	def publish(self):
		self.frappe.session.user = "Administrator"
		result = self.provider.publish_approved_options()
		self.frappe.session.user = "cashier@example.test"
		return result

	def option(self, *, item="VXN-DRY-GOAT", label="Chips"):
		for (doctype, _name), row in self.records.items():
			if doctype == self.provider.OPTION:
				snapshot = json.loads(row["snapshot"])
				if snapshot["item_code"] == item and snapshot["label"] == label:
					return Row(variation_id=int(row["public_id"]), name=label, price=float(snapshot["price"]))
		raise AssertionError("Missing fixture option")

	def selection(self, *options, quantity=None):
		options = options or (self.option(),)
		return {
			"modifier": [option.variation_id for option in options],
			"modifier_price": [option.price for option in options],
			"modifier_quantity": [self.line.quantity if quantity is None else quantity] * len(options),
		}

	def resolve(self, *, line=None, sale=None, selection=None, previous=None, location=None):
		return self.provider.provider(
			operation="resolve",
			company=self.company,
			location=location or self.location,
			sale=sale or self.sale,
			line=line or self.line,
			selection=selection if selection is not None else self.selection(),
			previous_line=previous,
		)

	def bind(self, result, *, line=None, sale=None):
		line = Row(deepcopy(line or self.line))
		sale = sale or self.sale
		payload = {
			"modifier": [row["variation_id"] for row in result["modifiers"]],
			"modifier_price": [row["price"] for row in result["modifiers"]],
			"modifier_quantity": [line.quantity] * len(result["modifiers"]),
			"modifiers": result["modifiers"],
		}
		if result["reference"]:
			payload[self.provider.PAYLOAD_FIELD] = {"version": 1, **result}
		line.line_payload = json.dumps(payload)
		sale.lines = [line]
		self.records[("Kit POS Sale", str(sale.name))] = sale.as_dict()
		return line

	def invoice(self):
		result = self.resolve()
		self.bind(result)
		self.sale.sales_invoice = "SI-1"
		self.records[("Kit POS Sale", str(self.sale.name))] = self.sale.as_dict()
		row = Row(
			name="SII-1",
			item_code=self.line.item_code,
			qty=self.line.quantity,
			uom="Pc(s)",
			stock_uom="Pc(s)",
			conversion_factor=1,
			rate=25000,
			warehouse="Kitchen - VXN",
			**{self.provider.INVOICE_FIELD: result["reference"]},
		)
		return Row(
			name="SI-1",
			doctype="Sales Invoice",
			company=self.company,
			pos_profile="Mini Bar",
			set_warehouse=self.location.warehouse,
			items=[row],
		), row

	@contextmanager
	def transaction(self):
		before, writes = deepcopy(self.records), list(self.writes)
		try:
			yield
		except Exception:
			self.records, self.writes = before, writes
			raise

	def test_publication_is_exact_idempotent_and_does_not_touch_parent_stock_or_bom(self):
		before = deepcopy(self.records)
		first = self.publish()
		self.assertEqual(first["options_created"], 36)
		second = self.publish()
		self.assertEqual((second["options_created"], second["options_existing"]), (0, 36))
		self.assertEqual(len(self.writes), 36)
		for key, value in before.items():
			self.assertEqual(self.records[key], value)
		for key in self.writes:
			raw = json.loads(self.records[key]["snapshot"])
			self.assertEqual(raw["stock_policy"], "sales_only")
			self.assertFalse({"qty", "stock_qty", "stock_item", "recipe", "bom"} & set(raw))

	def test_stock_parent_and_disabled_parent_can_be_published_before_launch(self):
		self.records[("Item", self.line.item_code)]["is_stock_item"] = 1
		self.assertEqual(self.publish()["options_created"], 36)
		self.assertEqual(self.records[("Item", self.line.item_code)]["disabled"], 1)

	def test_chicken_has_the_same_twelve_priced_choices_with_distinct_parent_ids(self):
		self.publish()
		for code in ("VXN-CHICKEN", "VXN-DRY-GOAT"):
			self.records[("Item", code)]["disabled"] = 0
		before = len(self.writes)
		catalogue = self.provider.provider(
			operation="catalogue", company=self.company, location=self.location,
			item_codes=["VXN-CHICKEN", "VXN-DRY-GOAT"],
		)
		self.assertEqual(self.provider.MENU["VXN-CHICKEN"], 15000)
		def labels(code):
			return {(group["name"], option["name"], option["price"])
				for group in catalogue[code] for option in group["modifiers"]}
		self.assertEqual(labels("VXN-CHICKEN"), labels("VXN-DRY-GOAT"))
		self.assertEqual(len(labels("VXN-CHICKEN")), 12)
		ids = {code: {option["variation_id"] for group in catalogue[code]
			for option in group["modifiers"]} for code in catalogue}
		self.assertFalse(ids["VXN-CHICKEN"] & ids["VXN-DRY-GOAT"])
		self.assertEqual(len(self.writes), before)

	def test_chicken_selection_is_replay_safe_and_cannot_reuse_goat_choices(self):
		self.publish()
		self.line.item_code = "VXN-CHICKEN"
		self.line.product_id = self.line.variation_id = 19
		selection = self.selection(self.option(item="VXN-CHICKEN"),
			self.option(item="VXN-CHICKEN", label="Extra Rice"))
		first = self.resolve(selection=selection)
		before = len(self.writes)
		self.assertEqual(self.resolve(selection=selection), first)
		self.assertEqual(len(first["modifiers"]), 2)
		with self.assertRaises(ApiError):
			self.resolve(selection=self.selection(self.option(item="VXN-DRY-GOAT")))
		self.assertEqual(len(self.writes), before)

	def test_existing_goat_modifier_ids_are_unchanged_by_chicken_publication(self):
		self.publish()
		self.assertEqual(self.option(item="VXN-DRY-GOAT").variation_id, 3451571183989807)
		self.assertEqual(self.option(item="VXN-PAN-FRIED-GOAT").variation_id, 3998074021553850)

	def test_fixed_recipe_bowls_do_not_gain_unrequested_modifier_charges(self):
		self.publish()
		before = len(self.writes)
		self.assertEqual(self.provider.provider(
			operation="catalogue", company=self.company, location=self.location,
			item_codes=["VXN-CHICKEN-BOWL", "VXN-GOAT-BOWL"],
		), {})
		self.assertEqual(len(self.writes), before)

	def test_publication_denies_cashier_and_unconfigured_company_without_writes(self):
		self.frappe.session.user = "cashier@example.test"
		with self.assertRaises(PermissionError):
			self.provider.publish_approved_options()
		self.frappe.session.user = "Administrator"
		self.frappe.conf = {}
		with self.assertRaises(ValueError):
			self.provider.publish_approved_options()
		self.assertEqual(self.writes, [])

	def test_all_parent_preconditions_are_checked_before_publication(self):
		self.records[("Item", "VXN-PAN-FRIED-GOAT")]["item_defaults"] = [Row(company="Other Company")]
		with self.assertRaises(self.provider.ModifierError):
			self.provider.publish_approved_options()
		self.assertEqual(self.writes, [])

	def test_public_catalogue_and_admin_preview_are_read_only_and_match_desktop_shape(self):
		self.publish()
		before = len(self.writes)
		self.frappe.session.user = "Administrator"
		preview = self.provider.preview_publications()
		self.assertEqual(set(preview), set(self.provider.MENU))
		for groups in preview.values():
			self.assertEqual([group["name"] for group in groups], ["Sides", "Extra Toppings"])
			for group in groups:
				self.assertEqual(set(group), {"id", "name", "modifiers"})
				self.assertEqual(len(group["modifiers"]), 6)
				self.assertTrue(0 < group["id"] < 2**53)
				for option in group["modifiers"]:
					self.assertEqual(set(option), {"variation_id", "name", "price"})
					self.assertTrue(0 < option["variation_id"] < 2**53)
					self.assertEqual(option["price"], 10000 if group["name"] == "Sides" else 5000)
		self.assertEqual(
			self.provider.provider(
				operation="catalogue",
				company=self.company,
				location=self.location,
				item_codes=list(self.provider.MENU),
			),
			{},
		)
		self.records[("Item", self.line.item_code)]["disabled"] = 0
		catalogue = self.provider.provider(
			operation="catalogue",
			company=self.company,
			location=self.location,
			item_codes=[self.line.item_code, "UNREQUESTED"],
		)
		self.assertEqual(set(catalogue), {self.line.item_code})
		self.assertEqual(len(self.writes), before)

	def test_unconfigured_companies_are_unchanged_even_with_bad_vox_configuration(self):
		self.frappe.conf[self.provider.CONFIG] = "broken"
		self.assertEqual(
			self.provider.provider(
				operation="catalogue", company="Other Company", location={}, item_codes=[self.line.item_code]
			),
			{},
		)
		with self.assertRaises(ValueError):
			self.provider.provider(
				operation="catalogue",
				company=self.company,
				location=self.location,
				item_codes=[self.line.item_code],
			)

	def test_missing_publication_is_not_created_by_catalogue(self):
		self.records[("Item", self.line.item_code)]["disabled"] = 0
		with self.assertRaises(ValueError):
			self.provider.provider(
				operation="catalogue",
				company=self.company,
				location=self.location,
				item_codes=[self.line.item_code],
			)
		self.assertEqual(self.writes, [])

	def test_selection_is_deterministic_before_save_and_replay_reuses_original_reference(self):
		self.publish()
		first = self.resolve()
		second = self.resolve()
		self.assertEqual(first, second)
		bound = self.bind(first)
		third = self.resolve(line=bound, previous=bound)
		self.assertEqual(first, third)
		self.assertEqual(len(self.writes), 37)

	def native_sale_name(self, sale=None):
		sale = sale or self.sale
		sale.name = int(sale.name)
		self.records[("Kit POS Sale", str(sale.name))] = sale.as_dict()
		return sale

	def test_native_names_accept_only_canonical_positive_integer_identities(self):
		for value in (1, "1", 2**53 - 1, str(2**53 - 1)):
			with self.subTest(value=value):
				self.assertEqual(self.provider._native_name(value), str(value))
		for value in (
			None,
			"",
			True,
			False,
			0,
			-1,
			1.0,
			"0",
			"-1",
			"01",
			"+1",
			"1.0",
			"1e0",
			" 1",
			"1 ",
			"\u0661",
			"\uff11\uff12",
			"SALE-1",
			2**53,
			str(2**53),
		):
			with self.subTest(value=value), self.assertRaises(self.provider.ModifierError):
				self.provider._native_name(value)

	def test_native_integer_sale_and_link_keep_snapshot_and_replay_identity(self):
		self.publish()
		self.native_sale_name()
		self.assertEqual(self.line.parent, "1")  # Stored child parent stays text.
		first = self.resolve()
		stored = self.records[(self.provider.SELECTION, first["reference"])]
		self.assertEqual(stored["kit_pos_sale"], 1)  # Simulated native Link validation.
		self.assertEqual(json.loads(stored["snapshot"])["sale"], "1")
		bound = self.bind(first)
		before = len(self.writes)
		self.assertEqual(self.resolve(line=bound, previous=bound), first)
		self.sale.name = "1"
		self.assertEqual(self.resolve(line=Row(bound, parent=1), previous=bound), first)
		self.assertEqual(len(self.writes), before)

	def test_same_bill_mixed_parent_types_allow_quantity_edit_not_cross_sale_merge(self):
		self.publish()
		self.native_sale_name()
		bound = self.bind(self.resolve())
		changed = Row(bound, parent=1, line_id="101", quantity=3)
		result = self.resolve(line=changed, previous=bound, selection=self.selection(quantity=3))
		snapshot = json.loads(self.records[(self.provider.SELECTION, result["reference"])]["snapshot"])
		self.assertEqual((snapshot["sale"], snapshot["line_id"], snapshot["quantity"]), ("1", "101", "3"))

	def test_numeric_name_normalization_preserves_db_uuid_and_parent_binding(self):
		self.publish()
		self.native_sale_name()
		before = len(self.writes)
		for parent in (2, "2", 0, False, "01", 1.0):
			with self.subTest(parent=parent), self.assertRaises(ApiError):
				self.resolve(line=Row(self.line, parent=parent))
		for uuid in ("different-uuid", 1, None):
			with self.subTest(uuid=uuid), self.assertRaises(ApiError):
				self.resolve(sale=Row(self.sale, sale_uuid=uuid))
		with self.assertRaises(ApiError):
			self.resolve(sale=Row(self.sale, name=2), line=Row(self.line, parent=2))
		self.assertEqual(len(self.writes), before)

	def test_native_link_conversion_cannot_change_sale_authority_during_insert(self):
		self.publish()
		self.native_sale_name()
		self.before_insert = lambda doc: setattr(doc, "kit_pos_sale", 2)
		with self.assertRaises(PermissionError):
			self.resolve()
		self.assertEqual(len(self.writes), 36)

	def test_disabled_opt_in_hides_catalogue_but_keeps_offline_and_existing_selections_valid(self):
		self.publish()
		self.frappe.conf[self.provider.CONFIG][self.company] = 0
		result = self.resolve()
		bound = self.bind(result)
		before = len(self.writes)
		self.assertEqual(
			self.provider.provider(
				operation="invoice",
				company=self.company,
				location=self.location,
				sale=self.sale,
				line=bound,
				reference=result["reference"],
			),
			result,
		)
		self.assertEqual(len(self.writes), before)
		self.assertEqual(
			self.provider.provider(
				operation="catalogue",
				company=self.company,
				location=self.location,
				item_codes=list(self.provider.MENU),
			),
			{},
		)

	def test_quantity_change_appends_a_version_and_keeps_old_selection_immutable(self):
		self.publish()
		first = self.resolve()
		bound = self.bind(first)
		before = deepcopy(self.records[(self.provider.SELECTION, first["reference"])])
		changed = Row(bound, quantity=3)
		second = self.resolve(line=changed, previous=bound, selection=self.selection(quantity=3))
		self.assertNotEqual(first["reference"], second["reference"])
		self.assertEqual(self.records[(self.provider.SELECTION, first["reference"])], before)
		raw = json.loads(self.records[(self.provider.SELECTION, second["reference"])]["snapshot"])
		self.assertEqual(raw["previous_selection"], first["reference"])
		self.assertEqual(raw["quantity"], "3")

	def test_explicit_clear_is_audited_once_and_returns_protocol_empty_arrays(self):
		self.publish()
		first = self.resolve()
		bound = self.bind(first)
		empty = {"modifier": [], "modifier_price": [], "modifier_quantity": []}
		self.assertEqual(
			self.resolve(line=bound, previous=bound, selection=empty), {"reference": "", "modifiers": []}
		)
		self.resolve(line=bound, previous=bound, selection=empty)
		self.assertEqual(len(self.writes), 38)

	def test_wrong_price_duplicate_id_unknown_id_and_wrong_parent_quantity_are_rejected(self):
		self.publish()
		mutations = [
			{"modifier_price": [9999]},
			{"modifier_quantity": [1]},
			{"modifier": [123]},
			{"modifier": [True]},
			{"modifier_price": [-1]},
			{"modifier_quantity": [float("nan")]},
		]
		for changes in mutations:
			with self.subTest(changes=changes), self.assertRaises(ApiError):
				self.resolve(selection={**self.selection(), **changes})
		with self.assertRaises(ApiError):
			self.resolve(selection=self.selection(self.option(), self.option()))
		self.assertEqual(len(self.writes), 36)

	def test_cross_parent_option_cannot_be_reused(self):
		self.publish()
		with self.assertRaises(ApiError):
			self.resolve(selection=self.selection(self.option(item="VXN-PAN-FRIED-GOAT")))

	def test_wrong_location_warehouse_company_and_guest_are_rejected(self):
		self.publish()
		for location in (
			Row(self.location, company="Other"),
			self.locations[200],
			Row(self.location, warehouse="Missing"),
		):
			with self.subTest(location=location), self.assertRaises(ApiError):
				self.resolve(location=location)
		self.frappe.session.user = "Guest"
		with self.assertRaises(ApiError):
			self.resolve()

	def test_changing_original_product_identity_uom_or_parent_breaks_frozen_proof(self):
		self.publish()
		first = self.resolve()
		bound = self.bind(first)
		for changes in ({"variation_id": 117}, {"sub_unit_id": 12}, {"quantity": 3}, {"parent": "OTHER"}):
			with self.subTest(changes=changes), self.assertRaises(ApiError):
				self.provider.provider(
					operation="invoice",
					company=self.company,
					location=self.location,
					sale=self.sale,
					line=Row(bound, **changes),
					reference=first["reference"],
				)

	def test_sales_uom_quantity_not_stock_multiplier_scales_modifiers(self):
		self.publish()
		line = Row(self.line, sub_unit_id=12)
		result = self.resolve(line=line)
		raw = json.loads(self.records[(self.provider.SELECTION, result["reference"])]["snapshot"])
		self.assertEqual((raw["uom"], raw["conversion_factor"], raw["quantity"]), ("Box", "12", "2"))
		with self.assertRaises(ApiError):
			self.resolve(line=line, selection=self.selection(quantity=24))

	def test_legitimate_same_opening_merge_preserves_source_and_binds_target(self):
		self.publish()
		first = self.resolve()
		bound = self.bind(first)
		target = self.seed(
			"Kit POS Sale",
			"2",
			sale_uuid="sale-uuid-2",
			location_id=100,
			pos_opening_entry="OPEN-1",
			materialize_state="draft",
			lines=[],
		)
		line = Row(bound, parent=target.name, line_id=202)
		moved = self.resolve(sale=target, line=line, previous=bound)
		raw = json.loads(self.records[(self.provider.SELECTION, moved["reference"])]["snapshot"])
		self.assertEqual((raw["sale"], raw["previous_selection"]), (target.name, first["reference"]))
		self.assertEqual(
			self.records[("Kit POS Sale", self.sale.name)]["lines"][0]["line_payload"], bound.line_payload
		)

	def test_merge_rejects_other_opening_missing_source_membership_or_already_merged_source(self):
		self.publish()
		first = self.resolve()
		bound = self.bind(first)
		target = self.seed(
			"Kit POS Sale",
			"2",
			sale_uuid="sale-uuid-2",
			location_id=100,
			pos_opening_entry="OPEN-2",
			materialize_state="draft",
			lines=[],
		)
		line = Row(bound, parent=target.name, line_id=202)
		with self.assertRaises(ApiError):
			self.resolve(sale=target, line=line, previous=bound)
		target.pos_opening_entry = "OPEN-1"
		self.records[("Kit POS Sale", self.sale.name)]["lines"] = []
		with self.assertRaises(ApiError):
			self.resolve(sale=target, line=line, previous=bound)
		self.bind(first)
		self.records[("Kit POS Sale", self.sale.name)]["merged_into"] = target.name
		with self.assertRaises(ApiError):
			self.resolve(sale=target, line=line, previous=bound)

	def merging_lines(self, *, source_label="Chips", target_label="Chips", target_qty=1):
		empty = {"reference": "", "modifiers": []}
		source_result = (
			self.resolve(selection=self.selection(self.option(label=source_label))) if source_label else empty
		)
		source_line = self.bind(source_result)
		target = self.seed(
			"Kit POS Sale",
			"2",
			sale_uuid="sale-uuid-2",
			location_id=100,
			pos_opening_entry="OPEN-1",
			materialize_state="draft",
			lines=[],
		)
		target_line = Row(self.line, parent=target.name, name="TARGET-LINE", line_id=202, quantity=target_qty)
		target_result = (
			self.resolve(
				sale=target,
				line=target_line,
				selection=self.selection(self.option(label=target_label), quantity=target_qty),
			)
			if target_label
			else empty
		)
		target_line = self.bind(target_result, sale=target, line=target_line)
		combined = Row(target_line, quantity=self.line.quantity + target_qty)
		selection = (
			self.selection(self.option(label=source_label), quantity=combined.quantity)
			if source_label
			else {
				"modifier": [],
				"modifier_price": [],
				"modifier_quantity": [],
			}
		)
		return target, combined, source_line, selection

	def test_combining_matching_choices_preserves_both_bills_and_is_replay_safe(self):
		self.publish()
		target, combined, source, selection = self.merging_lines()
		before = deepcopy(self.records)
		first = self.resolve(sale=target, line=combined, previous=source, selection=selection)
		second = self.resolve(sale=target, line=combined, previous=source, selection=selection)
		self.assertEqual(first, second)
		self.assertEqual(len(self.records), len(before) + 1)
		for key, record in before.items():
			self.assertEqual(self.records[key], record)

	def test_combining_different_choices_does_not_replace_either_bills_extras(self):
		self.publish()
		target, combined, source, selection = self.merging_lines(target_label="Rice")
		before = deepcopy(self.records)
		with self.assertRaises(ApiError):
			self.resolve(sale=target, line=combined, previous=source, selection=selection)
		self.assertEqual(self.records, before)

	def test_selected_source_cannot_spread_its_extras_onto_plain_target_portions(self):
		self.publish()
		target, combined, source, selection = self.merging_lines(target_label=None)
		before = deepcopy(self.records)
		with self.assertRaises(ApiError):
			self.resolve(sale=target, line=combined, previous=source, selection=selection)
		self.assertEqual(self.records, before)

	def test_plain_source_cannot_clear_extras_from_selected_target_portions(self):
		self.publish()
		target, combined, source, selection = self.merging_lines(source_label=None)
		before = deepcopy(self.records)
		with self.assertRaises(ApiError):
			self.resolve(sale=target, line=combined, previous=source, selection=selection)
		self.assertEqual(self.records, before)

	def test_plain_matching_merge_and_empty_target_adoption_remain_supported(self):
		self.publish()
		target, combined, source, selection = self.merging_lines(source_label=None, target_label=None)
		self.assertEqual(
			self.resolve(sale=target, line=combined, previous=source, selection=selection),
			{"reference": "", "modifiers": []},
		)
		target, combined, source, selection = self.merging_lines(target_label=None, target_qty=0)
		self.assertTrue(
			self.resolve(sale=target, line=combined, previous=source, selection=selection)["reference"]
		)

	def test_native_mixed_source_target_names_keep_merge_choices_and_quantity_guards(self):
		self.publish()
		self.native_sale_name()
		target, combined, source, selection = self.merging_lines()
		self.native_sale_name(target)
		combined.parent = target.name
		combined.line_id = str(combined.line_id)
		result = self.resolve(sale=target, line=combined, previous=source, selection=selection)
		snapshot = json.loads(self.records[(self.provider.SELECTION, result["reference"])]["snapshot"])
		self.assertEqual((snapshot["sale"], snapshot["quantity"]), ("2", "3"))
		with self.assertRaises(ApiError):
			self.resolve(
				sale=target,
				line=Row(combined, quantity=4),
				previous=source,
				selection=self.selection(quantity=4),
			)
		with self.assertRaises(ApiError):
			self.resolve(
				sale=target,
				line=combined,
				previous=source,
				selection=self.selection(self.option(label="Rice"), quantity=3),
			)

	def test_matching_merge_rejects_changed_incoming_source_choices_and_wrong_combined_quantity(self):
		self.publish()
		target, combined, source, selection = self.merging_lines()
		with self.assertRaises(ApiError):
			self.resolve(
				sale=target,
				line=combined,
				previous=source,
				selection=self.selection(self.option(label="Rice"), quantity=combined.quantity),
			)
		with self.assertRaises(ApiError):
			self.resolve(
				sale=target,
				line=Row(combined, quantity=4),
				previous=source,
				selection=self.selection(quantity=4),
			)

	def test_private_insert_edit_delete_and_rename_cannot_use_client_flags(self):
		self.publish()
		option = next(row for (doctype, _), row in self.records.items() if doctype == self.provider.OPTION)
		for new in (False, True):
			doc = Document(self, {**option, "__islocal": new, "ignore_permissions": True})
			with self.assertRaises(PermissionError):
				self.provider.validate_private_record(doc)
		with self.assertRaises(PermissionError):
			self.provider.prevent_private_change()

	def test_insert_context_rejects_tampering_and_does_not_leak_after_failure(self):
		self.publish()
		self.before_insert = lambda doc: doc.update(recorded_by="Attacker")
		with self.assertRaises(PermissionError):
			self.resolve()
		self.assertIsNone(self.provider._INSERT.get())
		self.assertEqual(len(self.writes), 36)

	def test_corrupt_snapshot_never_reprices_existing_selection(self):
		self.publish()
		first = self.resolve()
		bound = self.bind(first)
		option_name = json.loads(self.records[(self.provider.SELECTION, first["reference"])]["snapshot"])[
			"options"
		][0]["option"]
		stored = self.records[(self.provider.OPTION, option_name)]
		stored["snapshot"] = stored["snapshot"].replace('"price":"10000"', '"price":"1"')
		with self.assertRaises(ApiError):
			self.provider.provider(
				operation="invoice",
				company=self.company,
				location=self.location,
				sale=self.sale,
				line=bound,
				reference=first["reference"],
			)

	def test_caller_rollback_removes_selection_and_retry_is_safe(self):
		self.publish()
		with self.assertRaises(RuntimeError), self.transaction():
			self.resolve()
			raise RuntimeError("Later sale validation failed")
		self.assertEqual(len(self.writes), 36)
		self.resolve()
		self.assertEqual(len(self.writes), 37)
		self.assertFalse(hasattr(self.frappe.db, "commit"))

	def test_invoice_options_freeze_instructions_allow_row_kitchen_route_and_never_write(self):
		self.publish()
		invoice, row = self.invoice()
		before = len(self.writes)
		options = self.provider.invoice_options(invoice, row)
		self.assertEqual((options[0]["label"], options[0]["stock_policy"]), ("Chips", "sales_only"))
		self.assertNotIn("stock_qty", options[0])
		self.provider.validate_invoice_modifiers(invoice)
		self.assertEqual(len(self.writes), before)

	def test_invoice_without_selections_returns_empty_and_plain_other_company_is_unchanged(self):
		self.assertEqual(self.provider.invoice_options(Row(company=self.company), Row()), [])
		self.provider.validate_invoice_modifiers(Row(company="Other Company"))

	def test_invoice_completeness_detects_removed_copied_or_duplicate_reference(self):
		self.publish()
		invoice, row = self.invoice()
		reference = row[self.provider.INVOICE_FIELD]
		row[self.provider.INVOICE_FIELD] = ""
		with self.assertRaises(ValidationError):
			self.provider.validate_invoice_modifiers(invoice)
		row[self.provider.INVOICE_FIELD] = reference
		invoice["items"].append(Row(row, name="SII-2"))
		with self.assertRaises(ValidationError):
			self.provider.validate_invoice_modifiers(invoice)
		invoice["items"] = [row]
		invoice.name = "UNRELATED-INVOICE"
		with self.assertRaises(ValidationError):
			self.provider.validate_invoice_modifiers(invoice)

	def test_invoice_rejects_price_quantity_uom_and_header_tampering(self):
		self.publish()
		invoice, row = self.invoice()
		for field, changed in (("qty", 3), ("rate", 15000), ("uom", "Nos"), ("conversion_factor", 2)):
			old = row[field]
			row[field] = changed
			with self.subTest(field=field), self.assertRaises(self.provider.ModifierError):
				self.provider.invoice_options(invoice, row)
			row[field] = old
		invoice.set_warehouse = "Other Warehouse"
		with self.assertRaises(self.provider.ModifierError):
			self.provider.invoice_options(invoice, row)

	def test_trusted_invoice_context_allows_initial_link_but_not_wrong_sale(self):
		self.publish()
		invoice, row = self.invoice()
		self.records[("Kit POS Sale", self.sale.name)]["sales_invoice"] = ""
		with self.assertRaises(self.provider.ModifierError):
			self.provider.invoice_options(invoice, row)
		self.invoice_context = SimpleNamespace(sale=self.sale, location=self.location)
		self.assertEqual(len(self.provider.invoice_options(invoice, row)), 1)
		self.invoice_context = SimpleNamespace(sale=Row(name="WRONG"), location=self.location)
		with self.assertRaises(self.provider.ModifierError):
			self.provider.invoice_options(invoice, row)

	def test_native_context_and_linked_sale_name_types_preserve_invoice_verification(self):
		self.publish()
		self.native_sale_name()
		invoice, row = self.invoice()
		self.invoice_context = SimpleNamespace(sale=Row(self.sale, name="1"), location=self.location)
		before = len(self.writes)
		self.assertEqual(len(self.provider.invoice_options(invoice, row)), 1)
		self.provider.validate_invoice_modifiers(invoice)
		self.records[("Kit POS Sale", "1")]["sales_invoice"] = ""
		self.assertEqual(len(self.provider.invoice_options(invoice, row)), 1)
		self.provider.validate_invoice_modifiers(invoice)
		self.assertEqual(len(self.writes), before)

	def test_native_context_numeric_name_never_overrides_wrong_uuid_or_other_sale(self):
		self.publish()
		self.native_sale_name()
		invoice, row = self.invoice()
		for changes in ({"name": "2"}, {"name": "1", "sale_uuid": "wrong-uuid"}):
			self.invoice_context = SimpleNamespace(sale=Row(self.sale, **changes), location=self.location)
			with self.subTest(changes=changes):
				with self.assertRaises(self.provider.ModifierError):
					self.provider.invoice_options(invoice, row)
				with self.assertRaises(ValidationError):
					self.provider.validate_invoice_modifiers(invoice)

	def test_private_permissions_require_system_manager_and_company_read(self):
		doc = Row(company=self.company)
		self.frappe.session.user = "cashier@example.test"
		self.assertFalse(self.provider.private_has_permission(doc, permission_type="read"))
		self.assertEqual(self.provider.option_query_conditions(), "1 = 0")
		self.frappe.get_roles = lambda user: ["System Manager"]
		self.frappe.has_permission = lambda *args, **kwargs: True
		self.assertTrue(self.provider.private_has_permission(doc, permission_type="read"))
		self.assertFalse(self.provider.private_has_permission(doc, permission_type="write"))
		self.assertFalse(
			self.provider.private_has_permission(Row(company="Other Company"), permission_type="read")
		)
		self.assertIn(self.company, self.provider.selection_query_conditions())

	def test_schema_keeps_original_item_as_data_and_has_no_cashier_write_grants(self):
		root = Path(__file__).resolve().parents[1] / "fours_customizations" / "doctype"
		for folder in ("vox_kitchen_modifier_option", "vox_kitchen_modifier_selection"):
			definition = json.loads((root / folder / (folder + ".json")).read_text())
			fields = {field["fieldname"]: field for field in definition["fields"]}
			self.assertEqual(fields["item_code"]["fieldtype"], "Data")
			self.assertTrue(
				all(
					not row.get("write") and not row.get("create") and not row.get("delete")
					for row in definition["permissions"]
				)
			)
			self.assertTrue(fields["snapshot"]["read_only"])

	def setup_service(self, field="valid"):
		self.reloads, self.ensure_calls = [], []
		valid = Row(fieldtype="Data", hidden=1, read_only=1, no_copy=1, length=140)
		self.invoice_field = valid if field == "valid" else field
		self.frappe.get_meta = lambda doctype: SimpleNamespace(get_field=lambda name: self.invoice_field)
		self.frappe.reload_doc = lambda *args, **kwargs: self.reloads.append((args, kwargs))
		core = ModuleType("kit_pos.connector.modifiers")
		core.INVOICE_FIELD = self.provider.INVOICE_FIELD
		setup = ModuleType("kit_pos.setup")
		setup.__path__ = []
		modifiers = ModuleType("kit_pos.setup.modifiers")

		def ensure():
			self.ensure_calls.append(True)
			self.invoice_field = valid

		modifiers.ensure_schema = ensure
		path = Path(__file__).resolve().parents[1] / "vox_modifier_setup.py"
		name = "fours_customizations._isolated_vox_modifier_setup"
		spec = importlib.util.spec_from_file_location(name, path)
		module = importlib.util.module_from_spec(spec)
		self.enterContext(
			patch.dict(
				sys.modules,
				{
					"kit_pos.connector.modifiers": core,
					"kit_pos.setup": setup,
					"kit_pos.setup.modifiers": modifiers,
					name: module,
				},
			)
		)
		spec.loader.exec_module(module)
		return module

	def test_targeted_setup_verifies_existing_shared_field_without_overwriting_it(self):
		service = self.setup_service()
		before = deepcopy(self.records)
		result = service.install_schema()
		self.assertEqual(result["options_published"], 0)
		self.assertEqual(
			[entry[0][2] for entry in self.reloads],
			[
				"vox_kitchen_modifier_option",
				"vox_kitchen_modifier_selection",
			],
		)
		self.assertEqual(self.ensure_calls, [])
		self.assertEqual(self.records, before)

	def test_targeted_setup_installs_missing_opaque_field_through_existing_core_helper(self):
		service = self.setup_service(field=None)
		self.assertEqual(service.install_schema()["invoice_field_verified"], self.provider.INVOICE_FIELD)
		self.assertEqual(self.ensure_calls, [True])

	def test_incompatible_existing_shared_field_stops_before_any_schema_reload(self):
		for field in (
			Row(fieldtype="Link", hidden=1, read_only=1, no_copy=1, length=140),
			Row(fieldtype="Data", hidden=0, read_only=1, no_copy=1, length=140),
			Row(fieldtype="Data", hidden=1, read_only=1, no_copy=0, length=140),
			Row(fieldtype="Data", hidden=1, read_only=1, no_copy=1, length=20),
		):
			service = self.setup_service(field=field)
			with self.subTest(field=field), self.assertRaises(ValidationError):
				service.install_schema()
			self.assertEqual(self.reloads, [])
			self.assertEqual(self.ensure_calls, [])

	def test_targeted_schema_requires_admin_and_a_fresh_business_transaction(self):
		service = self.setup_service()
		self.frappe.session.user = "cashier@example.test"
		with self.assertRaises(PermissionError):
			service.install_schema()
		self.frappe.session.user = "Administrator"
		self.frappe.db.transaction_writes = 1
		with self.assertRaises(ValidationError):
			service.install_schema()
		self.assertEqual(self.reloads, [])

	def test_false_or_missing_company_opt_in_never_mints_options_from_read_paths(self):
		for value in ({}, {self.company: False}, {self.company: 0}):
			self.frappe.conf[self.provider.CONFIG] = value
			self.assertEqual(
				self.provider.provider(
					operation="catalogue",
					company=self.company,
					location=self.location,
					item_codes=list(self.provider.MENU),
				),
				{},
			)
		self.assertEqual(self.writes, [])

	def test_missing_schema_is_fail_closed_for_enabled_menu(self):
		self.schema.remove(self.provider.SELECTION)
		self.records[("Item", self.line.item_code)]["disabled"] = 0
		with self.assertRaises(ValueError):
			self.provider.provider(
				operation="catalogue",
				company=self.company,
				location=self.location,
				item_codes=list(self.provider.MENU),
			)
		self.assertEqual(self.writes, [])

"""U20 (ent-bom FC-1): Kit POS BOM production supersedes the Vox pilot.

Write-free: kit_pos's engine_policy is a stub module and the BOM tables are an
in-memory SQLite copy, so the helper's own SQL is what decides.
"""

import ast
import hashlib
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from fours_customizations import negative_stock_handler as stock
from fours_customizations import vox_catalogue_stock as catalogue
from fours_customizations import vox_work_orders as orders


COMPANY = "Vox Lounge Nansana"
SITE = "multax.kit.africa"


class Doc(SimpleNamespace):
	def get(self, name, default=None):
		return self.__dict__.get(name, default)


def policy(mode="on", supersedes=True):
	return SimpleNamespace(enabled=True, mode=mode, supersedes_vox_pilot=supersedes)


class KitPos:
	"""sys.modules entries for kit_pos.connector.production (or its absence)."""

	def __init__(self, engine_policy=None, *, missing_function=False, missing_package=False):
		self.engine_policy = Mock(side_effect=engine_policy) if callable(engine_policy) else Mock(
			return_value=engine_policy
		)
		if missing_package:
			self.modules = {"kit_pos": None, "kit_pos.connector": None, "kit_pos.connector.production": None}
			return
		root, connector, production = (
			ModuleType("kit_pos"), ModuleType("kit_pos.connector"), ModuleType("kit_pos.connector.production")
		)
		root.__path__, connector.__path__ = [], []
		if not missing_function:
			production.engine_policy = self.engine_policy
		root.connector, connector.production = connector, production
		self.modules = {"kit_pos": root, "kit_pos.connector": connector, "kit_pos.connector.production": production}

	def __enter__(self):
		self._patch = patch.dict(sys.modules, self.modules)
		self._patch.start()
		return self

	def __exit__(self, *exc):
		self._patch.stop()


class TestSupersedeSwitch(TestCase):
	"""Both pilot gates answer the same way; the engine calls both (DESIGN 4.1)."""

	def setUp(self):
		self.frappe = SimpleNamespace(local=SimpleNamespace(site=SITE), conf={orders.CONFIG: {COMPANY: True}})
		self.enterContext(patch.object(orders, "frappe", self.frappe))
		self.enterContext(patch.object(catalogue, "frappe", self.frappe))

	def enabled(self, company=COMPANY):
		answers = {orders._enabled(company), catalogue._enabled(company)}
		self.assertEqual(len(answers), 1)
		return answers.pop()

	def test_superseded_in_mode_on_and_paused(self):
		for mode in ("on", "paused"):
			with self.subTest(mode=mode), KitPos(policy(mode)) as kit:
				self.assertFalse(self.enabled())
				kit.engine_policy.assert_called_with(COMPANY)

	def test_mapping_policy_is_read_the_same_way(self):
		for mode in ("on", "paused"):
			with self.subTest(mode=mode), KitPos({"mode": mode, "supersedes_vox_pilot": True}):
				self.assertFalse(self.enabled())

	def test_engine_off_keeps_the_pilot(self):
		with KitPos(None):
			self.assertTrue(self.enabled())

	def test_policy_without_supersede_or_with_another_mode_keeps_the_pilot(self):
		for value in (policy("on", False), policy("paused", None), policy("on", 1), policy("off", True),
				policy(None, True), {"mode": "on"}, {"mode": "on", "supersedes_vox_pilot": "true"}):
			with self.subTest(policy=value), KitPos(value):
				self.assertTrue(self.enabled())

	def test_kit_pos_without_the_function_keeps_previous_behaviour(self):
		with KitPos(missing_function=True):
			self.assertTrue(self.enabled())
		with KitPos(missing_package=True):
			self.assertTrue(self.enabled())

	def test_failing_policy_read_keeps_previous_behaviour(self):
		def fail(company):
			raise ValueError("KPB-CONFIG_INVALID: malformed entry")

		with KitPos(fail) as kit:
			self.assertTrue(self.enabled())
			self.assertEqual(kit.engine_policy.call_count, 2)

	def test_pilot_off_or_out_of_scope_never_asks_kit_pos(self):
		with KitPos(policy("on")) as kit:
			self.frappe.conf = {}
			self.assertFalse(self.enabled())
			self.frappe.conf = {orders.CONFIG: {COMPANY: True}}
			self.assertFalse(self.enabled("Vox Lounge"))
			self.frappe.local.site = "ddamu.kit.africa"
			self.assertFalse(self.enabled())
			kit.engine_policy.assert_not_called()

	def test_malformed_pilot_config_still_raises(self):
		self.frappe.conf = {orders.CONFIG: {COMPANY: "true"}}
		with KitPos(policy("on")):
			with self.assertRaises(orders.ProductionError):
				orders._enabled(COMPANY)
			with self.assertRaises(ValueError):
				catalogue._enabled(COMPANY)

	def test_superseded_pilot_writes_nothing_but_still_guards_traces(self):
		invoice = Doc(company=COMPANY, items=[Doc(item_code="VXN-DRY-GOAT", warehouse="Main counter - VXN")])
		guard = self.enterContext(patch.object(orders, "guard_invoice_trace"))
		self.frappe.db = SimpleNamespace(get_value=Mock(), sql=Mock())
		with KitPos(policy("paused")):
			orders.route_invoice_warehouses(invoice)
			orders.ensure_invoice_production(invoice)
			orders.finalize_invoice_valuation(invoice)
			self.assertEqual(catalogue.catalogue_availability(
				company=COMPANY, location={"company": COMPANY}, item_codes=["VXN-DRY-GOAT"]), {})
		self.assertEqual(guard.call_count, 2)
		self.assertEqual(invoice.items[0].warehouse, "Main counter - VXN")
		self.frappe.db.get_value.assert_not_called()
		self.frappe.db.sql.assert_not_called()


# Recipes after the lead's 10-12 fixes, plus an old 4S copy and a retired dish
# (inactive), a draft and a cancelled BOM: none of those count.
BOMS = {
	"BOM-88759-002": ("88759", 1, 1, ["Raw Eggs"]),
	"BOM-88760-002": ("88760", 1, 1, ["Raw Eggs", "VX-0134-1"]),
	"BOM-88760-001": ("88760", 1, 0, ["Old Inactive Row"]),
	"BOM-Rice-002": ("Rice", 1, 1, ["Raw Rice"]),
	"BOM-VX-0134-1-001": ("VX-0134-1", 1, 1, ["Wheat Flour"]),
	"BOM-VX-0135-1-001": ("VX-0135-1", 1, 1, ["Minced Meat", "Wheat Flour"]),
	"BOM-VXN-DRY-GOAT-001": ("VXN-DRY-GOAT", 1, 1, ["VXN-RAW-GOAT-KG"]),
	"BOM-VXN-GOAT-PORTION-001": ("VXN-GOAT-PORTION", 1, 1, ["VXN-RAW-GOAT-KG"]),
	"BOM-RETIRED-001": ("Retired Dish", 1, 0, ["Retired Row"]),
	"BOM-DRAFT-001": ("Draft Dish", 0, 1, ["Draft Row"]),
	"BOM-CANCELLED-001": ("Cancelled Dish", 2, 1, ["Cancelled Row"]),
}


class BomTables:
	def __init__(self):
		self.db = sqlite3.connect(":memory:")
		self.db.execute("CREATE TABLE `tabBOM` (name, item, docstatus, is_active, company)")
		self.db.execute("CREATE TABLE `tabBOM Item` (parent, parenttype, parentfield, item_code)")
		for name, (item, docstatus, active, rows) in BOMS.items():
			company = "4S Industries Limited" if name == "BOM-88760-001" else COMPANY
			self.db.execute("INSERT INTO `tabBOM` VALUES (?, ?, ?, ?, ?)", (name, item, docstatus, active, company))
			for row in rows:
				self.db.execute("INSERT INTO `tabBOM Item` VALUES (?, 'BOM', 'items', ?)", (name, row))
		# A same-named child row under another parent type is not a recipe row.
		self.db.execute("INSERT INTO `tabBOM Item` VALUES ('BOM-VX-0134-1-001', 'Other', 'items', 'VX-DRINK')")
		self.calls = 0

	def sql(self, query, values=None, **kwargs):
		self.calls += 1
		codes = tuple(values["codes"])
		marks = "(" + ", ".join("?" * len(codes)) + ")"
		return self.db.execute(query.replace("%(codes)s", marks), codes * query.count("%(codes)s")).fetchall()


class TestNegativeStockHelper(TestCase):
	def setUp(self):
		from erpnext.stock import utils

		self.tables = BomTables()
		self.db = SimpleNamespace(get_value=Mock(side_effect=self._value), set_value=Mock(), sql=self.tables.sql)
		self.frappe = SimpleNamespace(db=self.db, local=SimpleNamespace(site=SITE), flags=Doc())
		self.enterContext(patch.object(stock, "frappe", self.frappe))
		self.enterContext(patch.object(stock, "get_settings", return_value=Doc(enable_negative_stock_automation=1)))
		self.balance = self.enterContext(patch.object(utils, "get_stock_balance", return_value=0))

	def _value(self, doctype, item, fields, **kwargs):
		if fields == "is_stock_item":
			return 1
		return Doc(allow_negative_stock=0, custom_negative_stock_auto_enabled=0)

	def sale(self, *codes, company=COMPANY):
		return Doc(company=company, set_warehouse="Main counter - VXN",
			items=[Doc(item_code=code, warehouse="Main counter - VXN", qty=1) for code in codes])

	def flagged(self):
		return {call.args[1] for call in self.db.set_value.call_args_list}

	def test_short_sale_of_a_recipe_item_or_an_ingredient_sets_no_flag(self):
		for code in ("VX-0134-1", "Wheat Flour", "88760", "Rice", "VXN-GOAT-PORTION", "VXN-RAW-GOAT-KG", "Raw Eggs"):
			with self.subTest(code=code):
				stock.ensure_negative_stock_for_doc(self.sale(code))
		self.balance.assert_not_called()
		self.db.set_value.assert_not_called()
		self.assertIsNone(self.frappe.flags.get("allow_negative_stock"))

	def test_short_sale_of_a_drink_still_sets_it(self):
		stock.ensure_negative_stock_for_doc(self.sale("VX-DRINK"))
		self.balance.assert_called_once_with("VX-DRINK", "Main counter - VXN")
		self.db.set_value.assert_any_call("Item", "VX-DRINK", "allow_negative_stock", 1, update_modified=False)
		self.db.set_value.assert_any_call("Item", "VX-DRINK", "custom_negative_stock_auto_enabled", 1, update_modified=False)
		self.assertTrue(self.frappe.flags.allow_negative_stock)

	def test_mixed_sale_flags_only_the_drink_with_one_query(self):
		stock.ensure_negative_stock_for_doc(self.sale("VX-0134-1", "VX-DRINK", "Wheat Flour", "88760", "VX-DRINK"))
		self.assertEqual(self.tables.calls, 1)
		self.assertEqual(self.flagged(), {"VX-DRINK"})

	def test_any_company_and_any_site(self):
		self.frappe.local.site = "ddamu.kit.africa"
		stock.ensure_negative_stock_for_doc(self.sale("VX-0134-1", "Wheat Flour", "VXN-DRY-GOAT", company="Other Co"))
		self.db.set_value.assert_not_called()

	def test_inactive_draft_and_cancelled_boms_do_not_exclude(self):
		codes = ("Old Inactive Row", "Retired Dish", "Retired Row", "Draft Dish", "Draft Row", "Cancelled Dish", "Cancelled Row")
		stock.ensure_negative_stock_for_doc(self.sale(*codes))
		self.assertEqual(self.flagged(), set(codes))

	def test_menu_only_sale_keeps_the_pilot_exclusion_without_a_query(self):
		stock.ensure_negative_stock_for_doc(self.sale("VXN-DRY-GOAT", "VXN-CHICKEN"))
		self.assertEqual(self.tables.calls, 0)
		self.balance.assert_not_called()
		self.db.set_value.assert_not_called()


class TestStockReconciliationFlags(TestCase):
	def setUp(self):
		self.tables = BomTables()
		self.db = SimpleNamespace(set_value=Mock(), sql=self.tables.sql)
		self.enterContext(patch.object(stock, "frappe", SimpleNamespace(db=self.db)))

	def writes(self):
		return {call.args[1]: call.args[2] for call in self.db.set_value.call_args_list}

	def reconcile(self, *codes):
		stock.on_stock_reconciliation_submit(Doc(docstatus=1, items=[Doc(item_code=code) for code in codes]))

	def test_ingredient_keeps_its_flag_and_dish_is_reset(self):
		self.reconcile("Wheat Flour", "VXN-DRY-GOAT")
		self.assertEqual(self.writes(), {
			"Wheat Flour": {"custom_negative_stock_auto_enabled": 0},
			"VXN-DRY-GOAT": {"allow_negative_stock": 0, "custom_negative_stock_auto_enabled": 0},
		})
		self.assertEqual(self.tables.calls, 1)

	def test_everything_else_is_reset_as_today(self):
		self.reconcile("VX-DRINK", "88760", "Old Inactive Row", "Draft Row", "Cancelled Row")
		self.assertTrue(all(values["allow_negative_stock"] == 0 for values in self.writes().values()))
		self.assertEqual(len(self.writes()), 5)

	def test_sub_assembly_is_a_row_and_keeps_its_flag(self):
		self.reconcile("VX-0134-1", "Raw Eggs", "VXN-RAW-GOAT-KG")
		self.assertTrue(all("allow_negative_stock" not in values for values in self.writes().values()))

	def test_draft_reconciliation_and_empty_rows_write_nothing(self):
		stock.on_stock_reconciliation_submit(Doc(docstatus=0, items=[Doc(item_code="VX-DRINK")]))
		stock.on_stock_reconciliation_submit(Doc(docstatus=1, items=[Doc(item_code=None)]))
		self.assertEqual(self.tables.calls, 0)
		self.db.set_value.assert_not_called()


# DESIGN 11.2 item 4: these pilot guards are byte-for-byte as at the production
# snapshot. A change here needs its own review, never FC-1's.
PROTECTED = {
	("vox_work_orders.py", "guard_generated_document"): "48a1a92d1d7a1c0df9324dd56a008cf3ef76162b894b01379fca43961adf8ac3",
	("vox_work_orders.py", "guard_invoice_trace"): "36b0d9325eff1b382a9a258f7d8742ca44864ab2ec7f2b497d04a93b12d1d1f3",
	("vox_pilot_credit.py", "guard_credit_source_write"): "f4a4e09f58c8c1d7e1895adc4686b87cd96b4bf17509b98cb712ff17f55f1023",
	("vox_pilot_credit.py", "guard_credit_approval"): "1c35a7e613d73fc2df004cb79703209be3864f95d417c5aa90eba0ba22d9f75a",
	("vox_pilot_credit.py", "guard_credit_invoice"): "0183eb1ee9204cbf711f088e3fa4f1bb8c03ad2b28e0d06deeb73613fba840af",
	("vox_pilot_credit.py", "verify_posted_credit"): "130a4ca1becf1b15dabe2099bb5a814d2b122f1d96a7f0b9ca8d51c791041985",
	("vox_pilot_credit.py", "is_exact_approved_credit"): "6b00837ec8ba7e68ed34246edd3ce10cfa55cb8c565ce8762ad09652fcc31756",
	("vox_modifiers.py", "provider"): "6830a1c5d910372643ab49fc26ba2096867cfe4f9a1a6d5f7341d52e23031b3a",
	("vox_modifiers.py", "validate_invoice_modifiers"): "5ad3386aa5336398e8af12476ca91f2ddf73511a78d41f6f76a4bfda3c0ae169",
}


class TestPilotGuardsUnchanged(TestCase):
	def test_protected_functions_are_unchanged(self):
		root = Path(__file__).resolve().parents[1]
		for (filename, name), digest in PROTECTED.items():
			with self.subTest(function=f"{filename}:{name}"):
				source = (root / filename).read_text()
				node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
				self.assertEqual(hashlib.sha256(ast.get_source_segment(source, node).encode()).hexdigest(), digest)

	def test_protected_hooks_are_still_wired(self):
		from fours_customizations import hooks

		for doctype in ("Work Order", "Stock Entry"):
			for event in ("validate", "before_submit", "before_update_after_submit", "before_cancel", "on_trash"):
				self.assertEqual(hooks.doc_events[doctype][event],
					"fours_customizations.vox_work_orders.guard_generated_document")
		events = hooks.doc_events["Sales Invoice"]
		wired = {path for paths in events.values() for path in ([paths] if isinstance(paths, str) else paths)}
		for path in ("fours_customizations.vox_work_orders.guard_invoice_trace",
				"fours_customizations.vox_pilot_credit.guard_credit_invoice",
				"fours_customizations.vox_modifiers.validate_invoice_modifiers"):
			self.assertIn(path, wired)
		self.assertEqual(hooks.kit_pos_modifier_provider, ["fours_customizations.vox_modifiers.provider"])
		self.assertEqual(hooks.doc_events["Stock Reconciliation"]["on_submit"],
			"fours_customizations.negative_stock_handler.on_stock_reconciliation_submit")

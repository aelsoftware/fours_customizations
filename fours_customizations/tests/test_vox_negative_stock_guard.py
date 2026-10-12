"""Write-free regressions for the scoped kitchen auto-flag exclusions."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from fours_customizations import negative_stock_handler as module


MENU = ("VXN-DRY-GOAT", "VXN-PAN-FRIED-GOAT", "VXN-CHICKEN", "VXN-CHICKEN-BOWL", "VXN-GOAT-BOWL")


class Doc(SimpleNamespace):
	def get(self, name, default=None):
		return self.__dict__.get(name, default)


class TestVoxNegativeStockGuard(TestCase):
	def setUp(self):
		from erpnext.stock import utils

		self.settings = Doc(enable_negative_stock_automation=1)
		self.db = SimpleNamespace(get_value=Mock(side_effect=self._value), set_value=Mock())
		self.frappe = SimpleNamespace(db=self.db, local=SimpleNamespace(site="multax.kit.africa"), flags=Doc())
		self.enterContext(patch.object(module, "frappe", self.frappe))
		self.enterContext(patch.object(module, "get_settings", return_value=self.settings))
		self.balance = self.enterContext(patch.object(utils, "get_stock_balance", return_value=0))

	def _value(self, doctype, item, fields, **kwargs):
		self.assertEqual(doctype, "Item")
		if fields == "is_stock_item":
			return 1
		return Doc(allow_negative_stock=0, custom_negative_stock_auto_enabled=0)

	def invoice(self, *codes, company="Vox Lounge Nansana"):
		return Doc(company=company, set_warehouse="Kitchen - VXN",
			items=[Doc(item_code=code, warehouse="Kitchen - VXN", qty=1) for code in codes])

	def flagged(self):
		return {call.args[1] for call in self.db.set_value.call_args_list}

	def test_all_dedicated_dishes_skip_every_legacy_stock_lookup_and_write(self):
		module.ensure_negative_stock_for_doc(self.invoice(*MENU))
		self.db.get_value.assert_not_called()
		self.db.set_value.assert_not_called()
		self.balance.assert_not_called()
		self.assertIsNone(self.frappe.flags.get("allow_negative_stock"))

	def test_lookup_failures_cannot_auto_flag_any_dedicated_dish(self):
		self.balance.side_effect = RuntimeError("stock balance lookup unavailable")
		for code in MENU:
			with self.subTest(code=code):
				module.ensure_negative_stock_for_doc(self.invoice(code))
		self.balance.assert_not_called()
		self.db.set_value.assert_not_called()
		self.assertIsNone(self.frappe.flags.get("allow_negative_stock"))

	def test_mixed_invoice_preserves_the_drink_negative_stock_behavior(self):
		module.ensure_negative_stock_for_doc(self.invoice(MENU[0], "VX-DRINK", MENU[1]))
		self.balance.assert_called_once_with("VX-DRINK", "Kitchen - VXN")
		self.assertEqual(self.flagged(), {"VX-DRINK"})
		self.assertEqual(self.db.set_value.call_count, 2)
		self.db.set_value.assert_any_call("Item", "VX-DRINK", "allow_negative_stock", 1, update_modified=False)
		self.db.set_value.assert_any_call("Item", "VX-DRINK", "custom_negative_stock_auto_enabled", 1, update_modified=False)
		self.assertTrue(self.frappe.flags.allow_negative_stock)

	def test_mixed_lookup_failure_preserves_only_the_unrelated_row_fallback(self):
		self.balance.side_effect = RuntimeError("stock balance lookup unavailable")
		module.ensure_negative_stock_for_doc(self.invoice(*MENU, "VX-DRINK"))
		self.balance.assert_called_once_with("VX-DRINK", "Kitchen - VXN")
		self.assertEqual(self.flagged(), {"VX-DRINK"})
		self.assertTrue(self.frappe.flags.allow_negative_stock)

	def test_same_items_in_another_company_keep_legacy_behavior(self):
		module.ensure_negative_stock_for_doc(self.invoice(*MENU, company="Vox Lounge"))
		self.assertEqual(self.balance.call_count, len(MENU))
		self.assertEqual(self.flagged(), set(MENU))
		self.assertTrue(self.frappe.flags.allow_negative_stock)

	def test_same_company_and_items_on_another_site_keep_legacy_behavior(self):
		self.frappe.local.site = "ddamu.kit.africa"
		module.ensure_negative_stock_for_doc(self.invoice(*MENU))
		self.assertEqual(self.balance.call_count, len(MENU))
		self.assertEqual(self.flagged(), set(MENU))
		self.assertTrue(self.frappe.flags.allow_negative_stock)

	def test_exclusion_does_not_apply_to_other_vox_stock_items(self):
		other_codes = ("VXN-RAW-GOAT-KG", "VXN-GOAT-PORTION", "VX-0128", "VX-DRINK")
		module.ensure_negative_stock_for_doc(self.invoice(*other_codes))
		self.assertEqual(self.balance.call_count, len(other_codes))
		self.assertEqual(self.flagged(), set(other_codes))

	def test_disabled_automation_remains_a_complete_noop(self):
		self.settings.enable_negative_stock_automation = 0
		module.ensure_negative_stock_for_doc(self.invoice(*MENU, "VX-DRINK"))
		self.db.get_value.assert_not_called()
		self.db.set_value.assert_not_called()
		self.balance.assert_not_called()
		self.assertIsNone(self.frappe.flags.get("allow_negative_stock"))

	def test_unrelated_sufficient_stock_does_not_create_flags(self):
		self.balance.return_value = 5
		module.ensure_negative_stock_for_doc(self.invoice(*MENU, "VX-DRINK"))
		self.balance.assert_called_once_with("VX-DRINK", "Kitchen - VXN")
		self.db.set_value.assert_not_called()
		self.assertIsNone(self.frappe.flags.get("allow_negative_stock"))

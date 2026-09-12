from __future__ import annotations

import configparser
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fours_customizations import vox_kit_pos

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class _PermissionDB:
	def __init__(self, *, applicable=True):
		self.applicable = applicable

	def exists(self, doctype, name):
		if doctype == "Role":
			return self.applicable and name == vox_kit_pos.VOX_ACCOUNTANT_ROLE
		if doctype == "Company":
			return self.applicable and name == "Vox Lounge Nansana"
		return False


class _ShiftDocument(SimpleNamespace):
	def __init__(self, saved, **values):
		super().__init__(**values)
		self._saved = saved

	def save(self, ignore_permissions=False):
		self._saved.append(
			{
				"shift_name": self.shift_name,
				"company": self.company,
				"start_time": self.start_time,
				"end_time": self.end_time,
				"is_default": self.is_default,
				"disabled": self.disabled,
				"ignore_permissions": ignore_permissions,
			}
		)


class _ShiftDB:
	def __init__(self):
		self.default_resets = []

	def exists(self, doctype, name):
		if doctype == "DocType":
			return name == "Kit POS Shift"
		if doctype == "Company":
			return name == "Vox Lounge Nansana"
		if doctype == "Kit POS Shift":
			return False
		return False

	def set_value(self, doctype, filters, fieldname, value, update_modified=False):
		self.default_resets.append(
			(doctype, filters, fieldname, value, update_modified)
		)


class TestVoxKitPos(TestCase):
	def test_patch_manifest_owns_both_vox_data_migrations(self):
		parser = configparser.ConfigParser(allow_no_value=True, delimiters="\n")
		parser.optionxform = str
		parser.read(PACKAGE_ROOT / "patches.txt")
		post = set(parser["post_model_sync"])

		self.assertIn(
			"fours_customizations.patches.grant_vox_sales_invoice_report_permission",
			post,
		)
		self.assertIn(
			"fours_customizations.patches.create_vox_bar_shifts",
			post,
		)

	def test_vox_permissions_delegate_generic_access_and_owned_grants(self):
		fake_frappe = SimpleNamespace(db=_PermissionDB(applicable=True))
		with (
			patch.object(vox_kit_pos, "frappe", fake_frappe),
			patch(
				"kit_pos.setup.voided_bill.setup_permissions"
			) as generic_permissions,
			patch.object(
				vox_kit_pos,
				"_grant_doctype_rights",
				side_effect=lambda doctype, rights: doctype != "Sales Invoice",
			) as grant,
			patch.object(vox_kit_pos, "_ensure_report_role", return_value=True),
		):
			result = vox_kit_pos.ensure_vox_kit_pos_permissions()

		generic_permissions.assert_called_once_with(
			roles=(vox_kit_pos.VOX_ACCOUNTANT_ROLE,)
		)
		self.assertEqual(
			{call.args[0]: call.args[1] for call in grant.call_args_list},
			vox_kit_pos.DOCTYPE_RIGHTS,
		)
		self.assertEqual(
			set(result["permissions_created"]), {"Kit POS Shift", "Voided Bill"}
		)
		self.assertTrue(result["report_role_created"])

	def test_non_vox_workspace_receives_no_vox_permissions(self):
		fake_frappe = SimpleNamespace(db=_PermissionDB(applicable=False))
		with (
			patch.object(vox_kit_pos, "frappe", fake_frappe),
			patch(
				"kit_pos.setup.voided_bill.setup_permissions"
			) as generic_permissions,
			patch.object(vox_kit_pos, "_grant_doctype_rights") as grant,
		):
			result = vox_kit_pos.ensure_vox_kit_pos_permissions()

		self.assertEqual(result["status"], "not_applicable")
		generic_permissions.assert_not_called()
		grant.assert_not_called()

	def test_shift_setup_keeps_original_vox_schedule_and_company_scope(self):
		saved = []
		db = _ShiftDB()
		fake_frappe = SimpleNamespace(
			db=db,
			new_doc=lambda doctype: _ShiftDocument(saved),
			get_doc=lambda *args: None,
			clear_cache=lambda **kwargs: None,
		)
		with patch.object(vox_kit_pos, "frappe", fake_frappe):
			result = vox_kit_pos.ensure_vox_bar_shifts()

		self.assertEqual(result["shifts"], ["Vox Lounge Nansana Bar Shift"])
		self.assertEqual(
			saved,
			[
				{
					"shift_name": "Vox Lounge Nansana Bar Shift",
					"company": "Vox Lounge Nansana",
					"start_time": "10:00:00",
					"end_time": "09:59:59",
					"is_default": 1,
					"disabled": 0,
					"ignore_permissions": True,
				}
			],
		)
		self.assertEqual(len(db.default_resets), 1)
		self.assertEqual(db.default_resets[0][1]["company"], "Vox Lounge Nansana")


if __name__ == "__main__":
	import unittest

	unittest.main()

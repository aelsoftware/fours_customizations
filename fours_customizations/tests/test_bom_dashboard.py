"""Standalone metadata-only tests: no site connection or document writes."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe
from erpnext.manufacturing.doctype.bom import bom_dashboard as native_dashboard

from fours_customizations import bom_dashboard, hooks


def _field(name, *, fieldtype="Link", options="BOM"):
	return SimpleNamespace(fieldname=name, fieldtype=fieldtype, options=options)


class _Meta:
	def __init__(self, *fields, children=()):
		self.fields = {field.fieldname: field for field in fields}
		self.children = [_field(name, fieldtype="Table", options=doctype) for name, doctype in children]

	def get_field(self, fieldname):
		return self.fields.get(fieldname)

	def get_table_fields(self, include_computed=False):
		return self.children


class TestBomDashboard(TestCase):
	def setUp(self):
		self.metadata = {
			"Item": _Meta(_field("default_bom")),
			"Stock Entry": _Meta(_field("bom_no")),
			"Quality Inspection": _Meta(_field("bom_no")),
			"BOM": _Meta(children=(("items", "BOM Item"),)),
			"BOM Item": _Meta(_field("bom_no")),
			"Work Order": _Meta(_field("bom_no")),
			"Job Card": _Meta(_field("bom_no")),
			"Purchase Order": _Meta(children=(("items", "Purchase Order Item"),)),
			"Purchase Order Item": _Meta(_field("bom")),
			"Purchase Receipt": _Meta(children=(("items", "Purchase Receipt Item"),)),
			"Purchase Receipt Item": _Meta(),
			"Purchase Invoice": _Meta(children=(("items", "Purchase Invoice Item"),)),
			"Purchase Invoice Item": _Meta(),
			"Subcontracting Order": _Meta(children=(("items", "Subcontracting Order Item"),)),
			"Subcontracting Order Item": _Meta(_field("bom")),
			"Subcontracting Receipt": _Meta(children=(("items", "Subcontracting Receipt Item"),)),
			"Subcontracting Receipt Item": _Meta(_field("bom")),
		}
		self.get_meta = self.enterContext(patch.object(bom_dashboard.frappe, "get_meta", self._get_meta))
		with patch.object(native_dashboard, "_", lambda text: text):
			self.data = native_dashboard.get_data()

	def _get_meta(self, doctype):
		if doctype not in self.metadata:
			raise frappe.DoesNotExistError(doctype)
		return self.metadata[doctype]

	def _items(self, data):
		return [target for group in data.get("transactions") or [] for target in group.get("items") or []]

	def test_hook_is_registered_only_for_bom(self):
		self.assertEqual(
			hooks.override_doctype_dashboards,
			{"BOM": "fours_customizations.bom_dashboard.get_dashboard_data"},
		)
		self.assertNotIn("frappe.desk.notifications.get_open_count", hooks.override_whitelisted_methods)

	def test_native_invalid_purchase_links_use_current_subcontracting_documents(self):
		before = deepcopy(self.data)
		result = bom_dashboard.get_dashboard_data(self.data)
		self.assertEqual(self.data, before)
		self.assertEqual(
			self._items(result),
			[
				"Item",
				"Stock Entry",
				"Quality Inspection",
				"BOM",
				"Work Order",
				"Job Card",
				"Purchase Order",
				"Subcontracting Order",
				"Subcontracting Receipt",
			],
		)
		self.assertEqual(result["fieldname"], "bom_no")
		self.assertEqual(result["transactions"][:2], before["transactions"][:2])
		self.assertEqual(result["non_standard_fieldnames"]["Item"], "default_bom")
		self.assertEqual(result["non_standard_fieldnames"]["Purchase Order"], "bom")
		for target in bom_dashboard.LEGACY_TARGETS:
			self.assertNotIn(target, result["non_standard_fieldnames"])
		for target in bom_dashboard.SUBCONTRACT_TARGETS:
			self.assertEqual(result["non_standard_fieldnames"][target], "bom")
			self.assertEqual(result["transactions"][2]["fieldnames"][target], "bom")
			self.assertIn(target, result["disable_create_buttons"])
		self.assertTrue(set(before["disable_create_buttons"]).issubset(result["disable_create_buttons"]))

	def test_every_corrected_external_filter_resolves_to_parent_or_child_metadata(self):
		result = bom_dashboard.get_dashboard_data(self.data)
		for target in self._items(result):
			with self.subTest(target=target):
				fieldname = result["non_standard_fieldnames"].get(target, result["fieldname"])
				self.assertTrue(bom_dashboard._has_field(target, fieldname))

	def test_native_counter_receives_the_same_bom_filter_as_list_clicks(self):
		from frappe.desk import notifications

		result = bom_dashboard.get_dashboard_data(self.data)
		for target in bom_dashboard.SUBCONTRACT_TARGETS:
			with (
				self.subTest(target=target),
				patch.object(notifications, "get_doc_count", return_value=2) as count,
				patch.object(notifications, "get_filters_for", return_value=None),
			):
				value = notifications.get_external_links(target, "BOM-VXN-DRY-GOAT-001", result)
				count.assert_called_once_with(target, {"bom": "BOM-VXN-DRY-GOAT-001"})
				self.assertEqual(value, {"doctype": target, "count": 2, "open_count": 0})
				self.assertEqual(result["transactions"][2]["fieldnames"][target], "bom")

	def test_legacy_parent_and_child_bom_relations_are_preserved(self):
		self.metadata["Purchase Receipt"] = _Meta(_field("bom"))
		self.metadata["Purchase Invoice Item"] = _Meta(_field("bom"))
		self.assertEqual(bom_dashboard.get_dashboard_data(self.data), self.data)

	def test_valid_custom_field_mappings_are_preserved(self):
		self.data["non_standard_fieldnames"]["Purchase Receipt"] = "custom_receipt_bom"
		self.data["non_standard_fieldnames"]["Purchase Invoice"] = "custom_invoice_bom"
		self.metadata["Purchase Receipt"] = _Meta(_field("custom_receipt_bom"))
		self.metadata["Purchase Invoice Item"] = _Meta(_field("custom_invoice_bom"))
		self.assertEqual(bom_dashboard.get_dashboard_data(self.data), self.data)

	def test_valid_custom_internal_relationships_are_preserved(self):
		self.data["internal_links"] = {"Purchase Receipt": ["custom_receipts", "purchase_receipt"]}
		self.data["internal_and_external_links"] = {"Purchase Invoice": "custom_purchase_invoice"}
		self.assertEqual(bom_dashboard.get_dashboard_data(self.data), self.data)

	def test_one_valid_legacy_target_is_kept_while_the_other_is_replaced(self):
		self.metadata["Purchase Receipt Item"] = _Meta(_field("bom"))
		result = bom_dashboard.get_dashboard_data(self.data)
		self.assertIn("Purchase Receipt", self._items(result))
		self.assertNotIn("Purchase Invoice", self._items(result))
		self.assertEqual(result["non_standard_fieldnames"]["Purchase Receipt"], "bom")

	def test_unrelated_custom_dashboard_data_and_group_filters_are_unchanged(self):
		self.data.update(
			{
				"reports": [{"name": "Custom BOM Report"}],
				"dynamic_links": {"custom_link": ["BOM", "reference_doctype"]},
				"custom_summary": {"nested": [1, 2]},
			}
		)
		self.data["transactions"][2].update(
			{
				"label": "Custom translated subcontract label",
				"fieldnames": {"Purchase Receipt": "bom", "Purchase Invoice": "bom", "Purchase Order": "bom"},
				"custom_note": "Keep this",
			}
		)
		before = deepcopy(self.data)
		result = bom_dashboard.get_dashboard_data(self.data)
		for key in ("reports", "dynamic_links", "custom_summary"):
			self.assertEqual(result[key], before[key])
		self.assertEqual(result["transactions"][2]["custom_note"], "Keep this")
		self.assertEqual(result["transactions"][2]["label"], "Custom translated subcontract label")
		self.assertEqual(result["transactions"][2]["fieldnames"]["Purchase Order"], "bom")
		self.assertNotIn("Purchase Receipt", result["transactions"][2]["fieldnames"])
		self.assertNotIn("Purchase Invoice", result["transactions"][2]["fieldnames"])
		result["custom_summary"]["nested"].append(3)
		self.assertEqual(self.data, before)

	def test_existing_subcontract_targets_are_not_duplicated_or_reconfigured(self):
		self.data["transactions"].append({"label": "Custom references", "items": ["Subcontracting Order"]})
		self.data["non_standard_fieldnames"]["Subcontracting Order"] = "custom_bom"
		self.metadata["Subcontracting Order"] = _Meta(_field("custom_bom"))
		result = bom_dashboard.get_dashboard_data(self.data)
		self.assertEqual(self._items(result).count("Subcontracting Order"), 1)
		self.assertEqual(result["non_standard_fieldnames"]["Subcontracting Order"], "custom_bom")
		self.assertEqual(result["transactions"][-1], self.data["transactions"][-1])

	def test_replacement_requires_an_installed_bom_link(self):
		self.metadata["Subcontracting Order Item"] = _Meta(_field("bom", options="Item"))
		self.metadata.pop("Subcontracting Receipt")
		result = bom_dashboard.get_dashboard_data(self.data)
		for target in bom_dashboard.SUBCONTRACT_TARGETS:
			self.assertNotIn(target, self._items(result))

	def test_missing_child_metadata_does_not_invent_a_replacement_relation(self):
		self.metadata.pop("Subcontracting Order Item")
		result = bom_dashboard.get_dashboard_data(self.data)
		self.assertNotIn("Subcontracting Order", self._items(result))
		self.assertIn("Subcontracting Receipt", self._items(result))

	def test_a_data_field_named_bom_is_not_mistaken_for_a_new_native_bom_link(self):
		self.metadata["Subcontracting Order Item"] = _Meta(_field("bom", fieldtype="Data", options=None))
		result = bom_dashboard.get_dashboard_data(self.data)
		self.assertNotIn("Subcontracting Order", self._items(result))

	def test_parent_field_priority_is_respected_when_a_custom_field_shadows_child_bom(self):
		self.metadata["Subcontracting Order"] = _Meta(
			_field("bom", fieldtype="Data", options=None),
			children=(("items", "Subcontracting Order Item"),),
		)
		result = bom_dashboard.get_dashboard_data(self.data)
		self.assertNotIn("Subcontracting Order", self._items(result))

	def test_first_matching_child_field_priority_is_respected(self):
		self.metadata["Subcontracting Order"] = _Meta(
			children=(("custom_rows", "Custom Row"), ("items", "Subcontracting Order Item")),
		)
		self.metadata["Custom Row"] = _Meta(_field("bom", fieldtype="Data", options=None))
		result = bom_dashboard.get_dashboard_data(self.data)
		self.assertNotIn("Subcontracting Order", self._items(result))

	def test_custom_count_method_is_not_reinterpreted_as_direct_native_relations(self):
		self.data["method"] = "another_app.bom_indirect_counts"
		with patch.object(bom_dashboard.frappe, "get_meta") as get_meta:
			self.assertEqual(bom_dashboard.get_dashboard_data(self.data), self.data)
		get_meta.assert_not_called()

	def test_no_legacy_targets_means_no_new_dashboard_links(self):
		self.data["transactions"] = self.data["transactions"][:2]
		with patch.object(bom_dashboard.frappe, "get_meta") as get_meta:
			self.assertEqual(bom_dashboard.get_dashboard_data(self.data), self.data)
		get_meta.assert_not_called()

	def test_empty_dashboard_is_left_empty(self):
		self.assertEqual(bom_dashboard.get_dashboard_data({}), {})

	def test_override_is_idempotent(self):
		result = bom_dashboard.get_dashboard_data(self.data)
		self.assertEqual(bom_dashboard.get_dashboard_data(result), result)

	def test_metadata_errors_are_not_swallowed_or_returned_as_false_zero_counts(self):
		with (
			patch.object(bom_dashboard.frappe, "get_meta", side_effect=RuntimeError("metadata unavailable")),
			self.assertRaisesRegex(RuntimeError, "metadata unavailable"),
		):
			bom_dashboard.get_dashboard_data(self.data)


if __name__ == "__main__":
	import unittest

	unittest.main()

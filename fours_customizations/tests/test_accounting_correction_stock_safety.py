from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from fours_customizations import accounting_correction as correction


def _invoice(**values):
	data = {
		"doctype": "Sales Invoice",
		"name": "TEST-STOCK-SI",
		"company": correction.FOUR_S_COMPANY,
		"posting_date": date(2026, 9, 16),
		"posting_time": timedelta(hours=12, minutes=34, seconds=56),
		"items": [
			frappe._dict(
				name="SI-ITEM-1",
				item_code="BRAKE-PEDAL",
				warehouse="Stores - 4S",
				stock_qty=10,
				serial_no=None,
				batch_no=None,
				serial_and_batch_bundle=None,
			)
		],
	}
	data.update(values)
	return frappe._dict(data)


def _source_sle(**values):
	data = {
		"name": "SLE-1",
		"docstatus": 1,
		"company": correction.FOUR_S_COMPANY,
		"posting_date": date(2026, 9, 16),
		"posting_time": timedelta(hours=12, minutes=34, seconds=56),
		"posting_datetime": datetime(2026, 9, 16, 12, 34, 56),
		"fiscal_year": "2026",
		"item_code": "BRAKE-PEDAL",
		"warehouse": "Stores - 4S",
		"actual_qty": -10,
		"qty_after_transaction": 90,
		"incoming_rate": 0,
		"outgoing_rate": 2_000,
		"valuation_rate": 2_000,
		"stock_value": 180_000,
		"stock_value_difference": -20_000,
		"stock_queue": "[[90.0, 2000.0]]",
		"voucher_type": "Sales Invoice",
		"voucher_no": "TEST-STOCK-SI",
		"voucher_detail_no": "SI-ITEM-1",
		"batch_no": None,
		"serial_no": None,
		"serial_and_batch_bundle": None,
		"has_serial_no": 0,
		"has_batch_no": 0,
		"dependant_sle_voucher_detail_no": None,
		"is_cancelled": 0,
		"is_adjustment_entry": 0,
		"recalculate_rate": 0,
		"creation": datetime(2026, 9, 16, 12, 35),
		"modified": datetime(2026, 9, 16, 12, 35),
		"modified_by": "Administrator",
	}
	data.update(values)
	return frappe._dict(data)


def _stock_plan():
	before = _source_sle()
	after = frappe._dict(
		{
			**dict(before),
			"posting_date": date(2026, 9, 17),
			"posting_datetime": datetime(2026, 9, 17, 12, 34, 56),
			"fiscal_year": "2027",
			"modified": datetime(2026, 9, 17, 9),
			"modified_by": "accountant@example.com",
		}
	)
	bin_snapshot = {
		"name": "BIN-1",
		"item_code": before.item_code,
		"warehouse": before.warehouse,
		"actual_qty": 90,
		"valuation_rate": 2_000,
		"stock_value": 180_000,
		"projected_qty": 90,
		"reserved_qty": 0,
	}
	external_ledgers = {
		"payment_ledger_entries": [{"name": "PLE-1", "amount": -20_000}],
		"advance_payment_ledger_entries": [],
	}
	plan = {
		"old_posting_date": "2026-09-16",
		"new_posting_date": "2026-09-17",
		"posting_time": "12:34:56",
		"old_posting_datetime": "2026-09-16 12:34:56",
		"new_posting_datetime": "2026-09-17 12:34:56",
		"new_fiscal_year": "2027",
		"company": correction.FOUR_S_COMPANY,
		"cutoff": "2026-09-16 12:34:56",
		"source_sles": [dict(before)],
		"source_sle_names": [before.name],
		"pairs": [{"item_code": before.item_code, "warehouse": before.warehouse}],
		"bin_before": [bin_snapshot],
		"pair_evidence": [
			{
				"item_code": before.item_code,
				"warehouse": before.warehouse,
				"terminal_sle": before.name,
				"reserved_stock_at_target": 0,
				"reservation_rows": [],
				"bin": {
					key: bin_snapshot[key]
					for key in (
						"name",
						"item_code",
						"warehouse",
						"actual_qty",
						"valuation_rate",
						"stock_value",
					)
				},
			}
		],
		"external_payment_ledgers_before": external_ledgers,
	}
	return plan, before, after, bin_snapshot, external_ledgers


class TestAccountingCorrectionStockSafety(TestCase):
	def test_apply_correction_rejects_disabled_transaction_control_before_locking(self):
		with (
			patch.object(correction.frappe.db, "_disable_transaction_control", 1),
			patch.object(correction, "_get_authorized_document") as get_doc,
			patch.object(correction.frappe.db, "rollback") as rollback,
			self.assertRaises(frappe.ValidationError),
		):
			correction.apply_correction(
				"Sales Invoice",
				"TEST-STOCK-SI",
				correction.CHANGE_POSTING_DATE,
				"Correct the posting date",
				expected_modified="2026-09-17 10:00:00",
				new_posting_date="2026-09-17",
			)

		get_doc.assert_not_called()
		rollback.assert_not_called()

	def test_apply_correction_leaves_successful_transaction_for_request_commit(self):
		doc = SimpleNamespace(modified="2026-09-17 10:00:00")
		expected = {"message": "done"}
		with (
			patch.object(correction, "_get_authorized_document", return_value=doc),
			patch.object(correction, "_apply_correction_transaction", return_value=expected),
			patch.object(correction.frappe.db, "rollback") as rollback,
		):
			result = correction.apply_correction(
				"Sales Invoice",
				"TEST-STOCK-SI",
				correction.CHANGE_POSTING_DATE,
				"Correct the posting date",
				expected_modified=doc.modified,
				new_posting_date="2026-09-17",
			)

		self.assertEqual(result, expected)
		rollback.assert_not_called()

	def test_apply_correction_full_rolls_back_and_reraises(self):
		doc = SimpleNamespace(modified="2026-09-17 10:00:00")
		error = frappe.ValidationError("fault after a write")
		with (
			patch.object(correction, "_get_authorized_document", return_value=doc),
			patch.object(correction, "_apply_correction_transaction", side_effect=error),
			patch.object(correction.frappe.db, "rollback") as rollback,
			self.assertRaises(frappe.ValidationError),
		):
			correction.apply_correction(
				"Sales Invoice",
				"TEST-STOCK-SI",
				correction.CHANGE_POSTING_DATE,
				"Correct the posting date",
				expected_modified=doc.modified,
				new_posting_date="2026-09-17",
			)

		rollback.assert_called_once_with()

	def test_stale_document_failure_rolls_back_after_locking(self):
		doc = SimpleNamespace(modified="2026-09-17 10:00:00")
		with (
			patch.object(correction, "_get_authorized_document", return_value=doc) as get_doc,
			patch.object(correction, "_apply_correction_transaction") as apply_transaction,
			patch.object(correction.frappe.db, "rollback") as rollback,
			self.assertRaises(frappe.ValidationError),
		):
			correction.apply_correction(
				"Sales Invoice",
				"TEST-STOCK-SI",
				correction.CHANGE_POSTING_DATE,
				"Correct the posting date",
				expected_modified="2026-09-17 09:59:59",
				new_posting_date="2026-09-17",
			)

		get_doc.assert_called_once_with("Sales Invoice", "TEST-STOCK-SI", for_update=True)
		apply_transaction.assert_not_called()
		rollback.assert_called_once_with()

	def test_apply_correction_surfaces_full_rollback_failure_as_terminal(self):
		doc = SimpleNamespace(modified="2026-09-17 10:00:00")
		original_error = frappe.ValidationError("fault after a write")
		rollback_error = RuntimeError("database connection lost")
		with (
			patch.object(correction, "_get_authorized_document", return_value=doc),
			patch.object(
				correction, "_apply_correction_transaction", side_effect=original_error
			),
			patch.object(correction.frappe.db, "rollback", side_effect=rollback_error) as rollback,
			self.assertRaises(RuntimeError) as raised,
		):
			correction.apply_correction(
				"Sales Invoice",
				"TEST-STOCK-SI",
				correction.CHANGE_POSTING_DATE,
				"Correct the posting date",
				expected_modified=doc.modified,
				new_posting_date="2026-09-17",
			)

		self.assertIs(raised.exception, rollback_error)
		self.assertIs(raised.exception.__cause__, original_error)
		rollback.assert_called_once_with()

	def test_stock_freeze_date_blocks_both_ends_without_role_bypass(self):
		settings = frappe._dict(stock_frozen_upto=date(2026, 9, 16), stock_frozen_upto_days=0)
		with (
			patch.object(correction.frappe, "get_cached_doc", return_value=settings),
			patch.object(correction.frappe, "get_roles") as get_roles,
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_open_stock_dates(date(2026, 9, 16), date(2026, 9, 17))

		get_roles.assert_not_called()

	def test_stock_age_freeze_blocks_old_transaction(self):
		settings = frappe._dict(stock_frozen_upto=None, stock_frozen_upto_days=5)
		with (
			patch.object(correction.frappe, "get_cached_doc", return_value=settings),
			patch.object(correction, "today", return_value="2026-09-17"),
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_open_stock_dates(date(2026, 9, 1), date(2026, 9, 2))

	def test_pending_or_failed_repost_item_valuation_blocks(self):
		pending = [frappe._dict(name="RIV-1", status="In Progress")]
		with (
			patch.object(correction.frappe.db, "sql", return_value=pending) as sql,
			self.assertRaises(frappe.ValidationError),
		):
			correction._lock_stock_repost_queue(correction.FOUR_S_COMPANY)

		query = sql.call_args.args[0]
		self.assertIn("company = %(company)s", query)
		self.assertIn("'Queued', 'In Progress', 'Failed'", query)
		self.assertIn("docstatus = 1", query)
		self.assertIn("FOR UPDATE", query)

	def test_pending_stock_closing_entry_blocks(self):
		pending = [frappe._dict(name="CBAL-1", status="Queued")]
		with (
			patch.object(correction.frappe.db, "sql", return_value=pending) as sql,
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_no_stock_closing_after(
				correction.FOUR_S_COMPANY, datetime(2026, 9, 16, 12, 34, 56)
			)

		query = sql.call_args.args[0]
		self.assertIn("'Queued', 'In Progress', 'Failed'", query)
		self.assertIn("docstatus = 1", query)
		self.assertIn("FOR UPDATE", query)

	def test_completed_stock_closing_entry_at_or_after_cutoff_blocks(self):
		closing = frappe._dict(name="CBAL-2", to_date=date(2026, 9, 30))

		def stock_closing_rows(query, *args, **kwargs):
			return [closing] if "status = 'Completed'" in query else []

		with (
			patch.object(correction.frappe.db, "sql", side_effect=stock_closing_rows) as sql,
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_no_stock_closing_after(
				correction.FOUR_S_COMPANY, datetime(2026, 9, 16, 12, 34, 56)
			)

		stock_closing_queries = [
			invocation.args[0]
			for invocation in sql.call_args_list
			if "FROM `tabStock Closing Entry`" in invocation.args[0]
		]
		self.assertEqual(len(stock_closing_queries), 2)
		completed_query = stock_closing_queries[1]
		self.assertIn("docstatus = 1", completed_query)
		self.assertIn("status = 'Completed'", completed_query)
		self.assertIn("to_date >= %(cutoff_date)s", completed_query)
		self.assertIn("FOR UPDATE", completed_query)

	def test_stock_source_topology_requires_company_docstatus_and_exact_datetime(self):
		doc = _invoice()
		valid_sle = _source_sle()
		item = frappe._dict(
			name="BRAKE-PEDAL",
			is_stock_item=1,
			has_serial_no=0,
			has_batch_no=0,
		)
		with (
			patch.object(correction.frappe.db, "get_all", return_value=[item]),
			patch.object(correction.frappe.db, "exists", return_value=False),
		):
			correction._validate_stock_source_topology(doc, [valid_sle])

			corruptions = {
				"company": {"company": "Another Company"},
				"docstatus": {"docstatus": 0},
				"posting_date": {"posting_date": date(2026, 9, 15)},
				"posting_time": {"posting_time": timedelta(hours=12, minutes=34, seconds=55)},
				"posting_datetime": {
					"posting_datetime": datetime(2026, 9, 16, 12, 34, 55)
				},
				"fiscal_year": {"fiscal_year": "2025"},
			}
			for label, values in corruptions.items():
				with self.subTest(corruption=label), self.assertRaises(frappe.ValidationError):
					correction._validate_stock_source_topology(
						doc, [_source_sle(**values)]
					)

	def test_stock_retime_writes_only_date_metadata(self):
		plan, _before, _after, _bin, _external = _stock_plan()
		doc = _invoice()
		with (
			patch.object(correction.frappe, "new_doc") as new_doc,
			patch.object(correction.frappe.db, "set_value") as set_value,
			patch.object(correction, "_verify_terminal_stock_state") as verify,
		):
			result = correction._retime_terminal_stock_entries(doc, plan)

		self.assertEqual(result["operation"], correction.STOCK_OPERATION)
		self.assertTrue(result["verified"])
		self.assertEqual(result["source_entries"][0]["name"], "SLE-1")
		new_doc.assert_not_called()
		set_value.assert_called_once_with(
			"Stock Ledger Entry",
			"SLE-1",
			{
				"posting_date": date(2026, 9, 17),
				"posting_time": timedelta(hours=12, minutes=34, seconds=56),
				"posting_datetime": datetime(2026, 9, 17, 12, 34, 56),
				"fiscal_year": "2027",
			},
		)
		verify.assert_called_once_with(doc, plan)

	def test_stock_verifier_rejects_every_non_date_sle_value_change(self):
		plan, before, after, bin_snapshot, external_ledgers = _stock_plan()
		doc = _invoice()
		allowed_changes = {
			"posting_date",
			"posting_datetime",
			"fiscal_year",
			"modified",
			"modified_by",
		}
		fields_to_preserve = [field for field in before if field not in allowed_changes]

		for fieldname in fields_to_preserve:
			with self.subTest(fieldname=fieldname):
				corrupt = frappe._dict(dict(after))
				if fieldname == "stock_queue":
					corrupt[fieldname] = "[[89.0, 2000.0]]"
				elif fieldname == "posting_time":
					corrupt[fieldname] = timedelta(hours=12, minutes=34, seconds=55)
				elif isinstance(corrupt.get(fieldname), int | float):
					corrupt[fieldname] = corrupt.get(fieldname) + 1
				elif corrupt.get(fieldname) is None:
					corrupt[fieldname] = "unexpected"
				elif isinstance(corrupt.get(fieldname), datetime):
					corrupt[fieldname] = corrupt.get(fieldname) + timedelta(seconds=1)
				else:
					corrupt[fieldname] = f"{corrupt.get(fieldname)}-changed"

				with (
					patch.object(
						correction, "_stock_snapshot_fields", return_value=tuple(before)
					),
					patch.object(correction, "_stock_source_snapshot", return_value=[corrupt]),
					patch.object(
						correction, "_locked_bin_snapshot", return_value=bin_snapshot
					),
					patch.object(correction, "_stock_pair_range", return_value=[corrupt]),
					patch.object(correction, "_locked_stock_reservations", return_value=[]),
					patch.object(
						correction,
						"_external_invoice_ledger_snapshot",
						return_value=external_ledgers,
					),
					self.assertRaises(frappe.ValidationError),
				):
					correction._verify_terminal_stock_state(doc, plan)

	def test_terminal_chain_validates_quantity_value_rate_and_fifo_queue(self):
		previous = frappe._dict(
			name="SLE-PREV",
			qty_after_transaction=100,
			valuation_rate=2_000,
			stock_value=200_000,
			stock_queue="[[100, 2000]]",
		)
		source = _source_sle()
		with (
			patch.object(correction.frappe.db, "sql", return_value=[previous]),
			patch.object(correction, "_stock_precisions", return_value=(6, 2, 5)),
			patch("erpnext.stock.utils.get_valuation_method", return_value="FIFO"),
		):
			terminal = correction._validate_terminal_stock_chain(
				item_code=source.item_code,
				warehouse=source.warehouse,
				company=correction.FOUR_S_COMPANY,
				new_datetime=datetime(2026, 9, 17, 12, 34, 56),
				pair_source=[source],
				voucher_no="TEST-STOCK-SI",
				reserved_stock=0,
			)

		self.assertEqual(terminal.name, source.name)

	def test_terminal_chain_rejects_each_stock_arithmetic_drift(self):
		previous = frappe._dict(
			name="SLE-PREV",
			qty_after_transaction=100,
			valuation_rate=2_000,
			stock_value=200_000,
			stock_queue="[[100, 2000]]",
		)
		corruptions = {
			"quantity": {"qty_after_transaction": 91},
			"stock_value": {"stock_value": 180_001},
			"valuation_rate": {"valuation_rate": 2_001},
			"queue_quantity": {"stock_queue": "[[89, 2000]]"},
			"queue_value": {"stock_queue": "[[90, 2001]]"},
		}
		for label, values in corruptions.items():
			with (
				self.subTest(corruption=label),
				patch.object(correction.frappe.db, "sql", return_value=[previous]),
				patch.object(correction, "_stock_precisions", return_value=(6, 2, 5)),
				patch("erpnext.stock.utils.get_valuation_method", return_value="FIFO"),
				self.assertRaises(frappe.ValidationError),
			):
				correction._validate_terminal_stock_chain(
					item_code="BRAKE-PEDAL",
					warehouse="Stores - 4S",
					company=correction.FOUR_S_COMPANY,
					new_datetime=datetime(2026, 9, 17, 12, 34, 56),
					pair_source=[_source_sle(**values)],
					voucher_no="TEST-STOCK-SI",
					reserved_stock=0,
				)

	def test_target_date_reservations_are_part_of_negative_stock_check(self):
		previous = frappe._dict(
			name="SLE-PREV",
			qty_after_transaction=100,
			valuation_rate=2_000,
			stock_value=200_000,
			stock_queue="[[100, 2000]]",
		)
		with (
			patch.object(correction.frappe.db, "sql", return_value=[previous]),
			patch.object(correction, "_stock_precisions", return_value=(6, 2, 5)),
			patch("erpnext.stock.utils.get_valuation_method", return_value="FIFO"),
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_terminal_stock_chain(
				item_code="BRAKE-PEDAL",
				warehouse="Stores - 4S",
				company=correction.FOUR_S_COMPANY,
				new_datetime=datetime(2026, 9, 17, 12, 34, 56),
				pair_source=[_source_sle()],
				voucher_no="TEST-STOCK-SI",
				reserved_stock=91,
			)

	def test_reservation_residual_and_lock_query_match_core_semantics(self):
		rows = [
			frappe._dict(
				name="SRE-1",
				reserved_qty=12,
				delivered_qty=2,
				transferred_qty=3,
				consumed_qty=1,
				creation=datetime(2026, 9, 17, 10),
			)
		]
		with (
			patch.object(correction.frappe.db, "sql", return_value=rows) as sql,
			patch.object(correction, "_stock_precisions", return_value=(6, 2, 5)),
		):
			actual = correction._locked_stock_reservations(
				"BRAKE-PEDAL", "Stores - 4S", datetime(2026, 9, 17, 12)
			)
			self.assertEqual(correction._net_reserved_stock(actual), 6)

		query = sql.call_args.args[0]
		self.assertIn("docstatus = 1", query)
		self.assertIn("creation <= %(new_datetime)s", query)
		self.assertIn("FOR UPDATE", query)

	def test_bin_must_equal_terminal_stock_ledger_values(self):
		terminal = _source_sle()
		valid = {
			"name": "BIN-1",
			"actual_qty": 90,
			"valuation_rate": 2_000,
			"stock_value": 180_000,
		}
		with patch.object(correction, "_stock_precisions", return_value=(6, 2, 5)):
			correction._validate_bin_matches_terminal_sle(valid, terminal)
			for fieldname in ("actual_qty", "valuation_rate", "stock_value"):
				with self.subTest(fieldname=fieldname), self.assertRaises(
					frappe.ValidationError
				):
					correction._validate_bin_matches_terminal_sle(
						{**valid, fieldname: valid[fieldname] + 1}, terminal
					)

	def test_stock_snapshot_covers_standard_and_custom_physical_fields(self):
		fields = correction._stock_snapshot_fields()
		for fieldname in (
			"stock_uom",
			"project",
			"to_rename",
			"auto_created_serial_and_batch_bundle",
		):
			self.assertIn(fieldname, fields)

	def test_audit_payload_records_stock_evidence_without_a_riv_reference(self):
		evidence = {
			"version": correction.STOCK_EVIDENCE_VERSION,
			"operation": correction.STOCK_OPERATION,
			"source_entries": [{"name": "SLE-1"}],
			"verified": True,
		}
		captured = {}
		log = SimpleNamespace(name="AUDIT-1", insert=MagicMock())

		def make_log(values):
			captured.update(values)
			return log

		doc = SimpleNamespace(
			doctype="Sales Invoice",
			name="TEST-STOCK-SI",
			company=correction.FOUR_S_COMPANY,
		)
		with (
			patch.object(correction, "now_datetime", return_value=datetime(2026, 9, 17, 10)),
			patch.object(correction.frappe, "get_doc", side_effect=make_log),
		):
			result = correction._create_audit_log(
				doc=doc,
				action=correction.CHANGE_POSTING_DATE,
				reason="Correct stock invoice date",
				key="unique-key",
				old_values={},
				new_values={},
				ledger_before={},
				ledger_after={},
				repost_reference="RAL-1",
				stock_evidence=evidence,
			)

		self.assertIs(result, log)
		self.assertEqual(captured["stock_operation"], correction.STOCK_OPERATION)
		self.assertEqual(frappe.parse_json(captured["stock_evidence"]), evidence)
		self.assertNotIn("stock_repost_reference", captured)

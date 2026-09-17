from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import call, patch

import frappe

from fours_customizations import accounting_correction as correction


class _PaymentEntry(SimpleNamespace):
	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)


class _SalesInvoice(SimpleNamespace):
	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)


def _payment_entry(**values):
	data = {
		"doctype": "Payment Entry",
		"name": "TEST-PE",
		"company": correction.FOUR_S_COMPANY,
		"clearance_date": None,
	}
	data.update(values)
	return _PaymentEntry(**data)


def _sales_invoice(**values):
	data = {
		"doctype": "Sales Invoice",
		"name": "TEST-SI",
		"company": correction.FOUR_S_COMPANY,
		"is_pos": 1,
		"is_consolidated": 0,
		"is_created_using_pos": 0,
		"pos_closing_entry": None,
		"write_off_amount": 0,
		"base_write_off_amount": 0,
		"loyalty_program": None,
		"redeem_loyalty_points": 0,
		"loyalty_points": 0,
		"loyalty_amount": 0,
		"items": [],
	}
	data.update(values)
	return _SalesInvoice(**data)


class TestPaymentEntryCorrectionSafety(TestCase):
	def test_both_payment_entry_actions_run_dependency_guard_before_ledger_snapshot(self):
		doc = _payment_entry(modified="2026-09-17 10:00:00")
		for action in (correction.CHANGE_POSTING_DATE, correction.CHANGE_MODE_OF_PAYMENT):
			with (
				self.subTest(action=action),
				patch.object(correction, "_get_authorized_document", return_value=doc),
				patch.object(correction.frappe.db, "exists", return_value=False),
				patch.object(
					correction,
					"_validate_payment_entry_correction_dependencies",
					side_effect=frappe.ValidationError("blocked"),
				) as guard,
				patch.object(correction, "_ledger_snapshot") as ledger_snapshot,
				self.assertRaises(frappe.ValidationError),
			):
				correction.apply_correction(
					"Payment Entry",
					doc.name,
					action,
					"A reviewed correction reason",
					expected_modified=doc.modified,
				)

			guard.assert_called_once_with(doc)
			ledger_snapshot.assert_not_called()

	def test_immutable_ledger_blocks_payment_entry_correction(self):
		doc = _payment_entry()
		with (
			patch.object(correction.frappe, "get_single_value", return_value=1),
			patch.object(correction, "_validate_payment_entry_not_reconciled") as bank_guard,
			patch.object(correction, "_validate_no_linked_exchange_gain_loss_journals") as egl_guard,
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_payment_entry_correction_dependencies(doc)

		bank_guard.assert_not_called()
		egl_guard.assert_not_called()

	def test_clearance_date_blocks_before_bank_transaction_lookup(self):
		doc = _payment_entry(clearance_date=date(2026, 9, 17))
		with (
			patch.object(correction.frappe.db, "sql") as sql,
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_payment_entry_not_reconciled(doc)

		sql.assert_not_called()

	def test_active_bank_transaction_allocation_blocks_correction(self):
		doc = _payment_entry()
		allocation = frappe._dict(name="BTP-1", parent="BT-1")
		original_sql = correction.frappe.db.sql

		def sql_result(query, *args, **kwargs):
			if "tabBank Transaction Payments" in query:
				return [allocation]
			return original_sql(query, *args, **kwargs)

		with (
			patch.object(correction.frappe.db, "sql", side_effect=sql_result) as sql,
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_payment_entry_not_reconciled(doc)

		queries = "\n".join(call.args[0] for call in sql.call_args_list)
		self.assertIn("bt.docstatus < 2", queries)
		self.assertIn("FOR UPDATE", queries)

	def test_submitted_system_exchange_gain_loss_journal_blocks_correction(self):
		doc = _payment_entry()
		with (
			patch.object(
				correction.frappe.db,
				"sql",
				return_value=[frappe._dict(name="ACC-JV-2026-00001")],
			) as sql,
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_no_linked_exchange_gain_loss_journals(doc)

		query = sql.call_args.args[0]
		self.assertIn("je.docstatus = 1", query)
		self.assertIn("je.is_system_generated = 1", query)
		self.assertIn("Exchange Gain Or Loss", query)
		self.assertIn("FOR UPDATE", query)

	def test_direct_freeze_check_does_not_consult_exempt_roles(self):
		doc = _payment_entry()
		with (
			patch.object(correction.frappe.db, "get_value", return_value=date(2026, 9, 10)),
			patch.object(correction.frappe, "get_roles") as get_roles,
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_correction_date_is_open(doc, date(2026, 9, 10), is_opening=False)

		get_roles.assert_not_called()

	def test_direct_period_closing_voucher_check_cannot_be_bypassed(self):
		doc = _payment_entry()
		with (
			patch.object(correction.frappe.db, "get_value", return_value=None),
			patch.object(
				correction.frappe.db,
				"sql",
				return_value=[frappe._dict(period_end_date=date(2026, 9, 10))],
			),
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_correction_date_is_open(doc, date(2026, 9, 10), is_opening=False)

	def test_direct_accounting_period_check_ignores_exempted_role(self):
		doc = _payment_entry()
		with (
			patch.object(correction.frappe.db, "get_value", return_value=None),
			patch.object(
				correction.frappe.db,
				"sql",
				side_effect=[
					[frappe._dict(period_end_date=None)],
					[frappe._dict(name="Closed September")],
				],
			),
			patch.object(correction.frappe, "get_roles", return_value=[correction.ACCOUNTANT_ROLE]),
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_correction_date_is_open(doc, date(2026, 9, 17), is_opening=False)

	def test_old_and_new_dates_run_direct_and_standard_period_checks(self):
		doc = _payment_entry(is_opening="No")
		old_date = date(2026, 9, 16)
		new_date = date(2026, 9, 17)
		with (
			patch.object(correction, "_validate_correction_date_is_open") as direct_check,
			patch("erpnext.accounts.general_ledger.check_freezing_date") as standard_freeze,
			patch("erpnext.accounts.general_ledger.validate_against_pcv") as standard_pcv,
			patch("erpnext.accounts.general_ledger.validate_accounting_period") as standard_period,
		):
			correction._validate_open_accounting_dates(doc, old_date, new_date)

		direct_check.assert_has_calls(
			[call(doc, old_date, False), call(doc, new_date, False)], any_order=True
		)
		standard_freeze.assert_has_calls(
			[
				call(old_date, correction.FOUR_S_COMPANY),
				call(new_date, correction.FOUR_S_COMPANY),
			],
			any_order=True,
		)
		self.assertEqual(standard_pcv.call_count, 2)
		self.assertEqual(standard_period.call_count, 2)

	def test_sales_invoice_payment_clearance_blocks_before_bank_lookup(self):
		doc = _SalesInvoice(
			name="TEST-SI",
			payments=[frappe._dict(idx=2, clearance_date=date(2026, 9, 17))],
		)
		with (
			patch.object(correction.frappe.db, "sql") as sql,
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_sales_invoice_not_reconciled(doc)

		sql.assert_not_called()

	def test_active_sales_invoice_bank_transaction_allocation_blocks_correction(self):
		doc = _SalesInvoice(name="TEST-SI", payments=[])
		allocation = frappe._dict(name="BTP-1", parent="BT-1")
		with (
			patch.object(correction.frappe.db, "sql", return_value=[allocation]) as sql,
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_sales_invoice_not_reconciled(doc)

		query = sql.call_args.args[0]
		self.assertIn("payment_document = 'Sales Invoice'", query)
		self.assertIn("FOR UPDATE", query)

	def test_sales_invoice_dependency_guard_checks_immutable_period_and_reconciliation(self):
		doc = _SalesInvoice(posting_date=date(2026, 9, 17))
		with (
			patch.object(correction, "_validate_immutable_ledger_is_disabled") as immutable,
			patch.object(correction, "_validate_open_accounting_dates") as period,
			patch.object(correction, "_validate_sales_invoice_not_reconciled") as reconciliation,
			patch.object(correction, "_validate_sales_invoice_operational_dependencies") as operational,
		):
			correction._validate_sales_invoice_correction_dependencies(
				doc, correction.EXCLUDE_PAYMENT
			)

		immutable.assert_called_once_with()
		period.assert_called_once_with(doc, doc.posting_date, doc.posting_date)
		reconciliation.assert_called_once_with(doc)
		operational.assert_called_once_with(doc, correction.EXCLUDE_PAYMENT)

	def test_include_exclude_reject_unsafe_sales_invoice_dependencies(self):
		unsafe_documents = (
			_sales_invoice(write_off_amount=1),
			_sales_invoice(base_write_off_amount=1),
			_sales_invoice(is_consolidated=1),
			_sales_invoice(is_created_using_pos=1),
			_sales_invoice(pos_closing_entry="POS-CLOSE-1"),
			_sales_invoice(redeem_loyalty_points=1),
			_sales_invoice(loyalty_amount=100),
			_sales_invoice(items=[frappe._dict(asset="ASSET-1")]),
			_sales_invoice(items=[frappe._dict(is_fixed_asset=1)]),
		)
		for action in (correction.INCLUDE_PAYMENT, correction.EXCLUDE_PAYMENT):
			for doc in unsafe_documents:
				with (
					self.subTest(action=action, values=doc.__dict__),
					patch.object(correction.frappe.db, "sql", return_value=[]),
					self.assertRaises(frappe.ValidationError),
				):
					correction._validate_sales_invoice_operational_dependencies(doc, action)

	def test_ordinary_legacy_pos_invoice_is_supported(self):
		doc = _sales_invoice(is_pos=1, loyalty_program="Rewards", redeem_loyalty_points=0)
		with patch.object(correction.frappe.db, "sql", return_value=[]):
			correction._validate_sales_invoice_operational_dependencies(
				doc, correction.EXCLUDE_PAYMENT
			)

	def test_linked_submitted_system_journal_blocks_include_exclude(self):
		doc = _sales_invoice()
		with (
			patch.object(
				correction.frappe.db,
				"sql",
				return_value=[frappe._dict(name="ACC-JV-COMMON-PARTY")],
			) as sql,
			self.assertRaises(frappe.ValidationError),
		):
			correction._validate_sales_invoice_operational_dependencies(
				doc, correction.EXCLUDE_PAYMENT
			)

		query = sql.call_args.args[0]
		self.assertIn("jea.reference_type = 'Sales Invoice'", query)
		self.assertIn("je.docstatus = 1", query)
		self.assertIn("je.is_system_generated = 1", query)
		self.assertIn("FOR UPDATE", query)

	def test_active_voucher_ledgers_are_locked_before_snapshot(self):
		doc = _SalesInvoice(doctype="Sales Invoice", name="TEST-SI", update_stock=1)
		with patch.object(correction.frappe.db, "sql") as sql:
			correction._lock_active_ledger_rows(doc)

		self.assertEqual(sql.call_count, 4)
		queries = "\n".join(call.args[0] for call in sql.call_args_list)
		for table in (
			"tabGL Entry",
			"tabPayment Ledger Entry",
			"tabAdvance Payment Ledger Entry",
			"tabStock Ledger Entry",
		):
			self.assertIn(table, queries)
		self.assertEqual(queries.count("FOR UPDATE"), 4)

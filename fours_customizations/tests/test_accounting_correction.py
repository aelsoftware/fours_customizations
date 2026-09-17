from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import call, patch

import frappe

from fours_customizations import accounting_correction as correction
from fours_customizations.fours_customizations.doctype.four_s_accounting_correction.four_s_accounting_correction import (
	FourSAccountingCorrection,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class _Invoice(SimpleNamespace):
	def precision(self, fieldname):
		return 2

	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)

	def set(self, fieldname, value):
		setattr(self, fieldname, value)

	def append(self, fieldname, value):
		row = frappe._dict(value)
		getattr(self, fieldname).append(row)
		return row

	def get_db_value(self, fieldname):
		return getattr(self, "_db_values", {}).get(fieldname, getattr(self, fieldname, None))

	def set_payment_schedule(self):
		self.set_payment_schedule_calls = getattr(self, "set_payment_schedule_calls", 0) + 1
		if self.get("payment_schedule"):
			return
		amount = self.get(
			"grand_total", self.get("paid_amount", 0) + self.get("outstanding_amount", 0)
		)
		base_amount = self.get(
			"base_grand_total",
			correction.flt(amount) * correction.flt(self.get("conversion_rate") or 1),
		)
		self.append(
			"payment_schedule",
			{
				"idx": 1,
				"due_date": self.get("due_date"),
				"invoice_portion": 100,
				"payment_amount": amount,
				"base_payment_amount": base_amount,
				"outstanding": amount,
				"base_outstanding": base_amount,
			},
		)


class _PaymentEntry(SimpleNamespace):
	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)

	def set(self, fieldname, value):
		setattr(self, fieldname, value)

	def get_db_value(self, fieldname):
		return getattr(self, "_db_values", {}).get(fieldname, getattr(self, fieldname, None))

	def set_remarks(self):
		self.set_remarks_calls = getattr(self, "set_remarks_calls", 0) + 1
		self.remarks = " | ".join(
			str(value or "")
			for value in (
				self.get("mode_of_payment"),
				self.get("paid_from"),
				self.get("paid_to"),
				self.get("reference_no"),
			)
		)

	def set_title(self):
		self.set_title_calls = getattr(self, "set_title_calls", 0) + 1
		self.title = f"{self.paid_from} - {self.paid_to}"


class _StateDoc(SimpleNamespace):
	def as_dict(self, no_nulls=False):
		return dict(self.state)


def _invoice(*, paid_amount=1_280_000, conversion_rate=1):
	base_paid_amount = correction.flt(paid_amount * conversion_rate, 2)
	return _Invoice(
		doctype="Sales Invoice",
		name="TEST-SI",
		company=correction.FOUR_S_COMPANY,
		currency="UGX",
		company_currency="UGX",
		party_account_currency="UGX",
		debit_to="1310 - Debtors - 4S",
		conversion_rate=conversion_rate,
		paid_amount=paid_amount,
		base_paid_amount=base_paid_amount,
		change_amount=0,
		base_change_amount=0,
		outstanding_amount=0,
		account_for_change_amount="1110 - Cash - 4S",
		is_return=0,
		return_against=None,
		update_outstanding_for_self=0,
		is_opening="No",
		payment_terms_template="",
		payment_schedule=[],
		due_date=date(2026, 9, 30),
		grand_total=paid_amount,
		base_grand_total=base_paid_amount,
		payments=[
			frappe._dict(
				name="ROW-1",
				mode_of_payment="Cash Ugx",
				account="1110 - Cash - 4S",
				amount=paid_amount,
				base_amount=base_paid_amount,
			)
		],
	)


def _unpaid_invoice(*, outstanding_amount=250_000, conversion_rate=1.25, is_pos=0):
	return _Invoice(
		doctype="Sales Invoice",
		name="TEST-SI",
		company=correction.FOUR_S_COMPANY,
		currency="USD",
		company_currency="UGX",
		party_account_currency="USD",
		debit_to="1310 - Debtors - 4S",
		conversion_rate=conversion_rate,
		is_pos=is_pos,
		paid_amount=0,
		base_paid_amount=0,
		change_amount=99,
		base_change_amount=124,
		outstanding_amount=outstanding_amount,
		is_return=0,
		is_opening="No",
		payment_terms_template="",
		payment_schedule=[],
		due_date=date(2026, 9, 30),
		grand_total=outstanding_amount,
		base_grand_total=correction.flt(outstanding_amount * conversion_rate, 2),
		payments=[],
	)


def _payment_entry(*, payment_type="Receive"):
	amounts = {
		fieldname: index + 10 for index, fieldname in enumerate(correction.PAYMENT_ENTRY_AMOUNT_FIELDS)
	}
	return _PaymentEntry(
		doctype="Payment Entry",
		name="TEST-PE",
		company=correction.FOUR_S_COMPANY,
		posting_date=date(2026, 9, 16),
		status="Submitted",
		payment_type=payment_type,
		mode_of_payment="Cash Ugx",
		paid_from="Debtors - 4S" if payment_type == "Receive" else "1110 - Cash - 4S",
		paid_to="1110 - Cash - 4S" if payment_type == "Receive" else "Creditors - 4S",
		paid_from_account_currency="UGX",
		paid_to_account_currency="UGX",
		paid_from_account_type="Receivable" if payment_type == "Receive" else "Cash",
		paid_to_account_type="Cash" if payment_type == "Receive" else "Payable",
		reference_no="OLD-REF",
		reference_date=date(2026, 9, 1),
		bank_account="OLD-BANK-ACCOUNT",
		bank="Old Bank",
		bank_account_no="OLD-001",
		clearance_date=None,
		custom_remarks=1,
		remarks="Existing remarks",
		title="Existing title",
		_db_values={"mode_of_payment": "Cash Ugx"},
		**amounts,
	)


def _mode(name):
	accounts = {
		"Cash Ugx": "1110 - Cash - 4S",
		"MTN Mobile Money": "MTN Mobile Money - 4S",
		"Bank Transfer": "Bank - 4S",
	}
	return frappe._dict(
		name=name,
		type={"Cash Ugx": "Cash", "MTN Mobile Money": "Phone", "Bank Transfer": "Bank"}[name],
		account=accounts[name],
		account_currency="UGX",
		account_type="Bank" if name == "Bank Transfer" else "Cash",
	)


def _ledger_snapshot(*, gl_entries=None, payment_entries=None, advance_entries=None, stock_entries=None):
	gl_entries = gl_entries or []
	return {
		"gl_entries": gl_entries,
		"gl_debit": sum(row.get("debit", 0) for row in gl_entries),
		"gl_credit": sum(row.get("credit", 0) for row in gl_entries),
		"payment_ledger_entries": payment_entries or [],
		"advance_payment_ledger_entries": advance_entries or [],
		"stock_ledger_entries": stock_entries or [],
	}


class TestAccountingCorrection(TestCase):
	def test_payment_mode_can_be_split_but_total_is_locked(self):
		doc = _invoice()
		with (
			patch.object(correction, "_get_mode_of_payment", side_effect=lambda company, name: _mode(name)),
			patch.object(correction, "_validate_invoice_payment_account_currency"),
		):
			details = correction._replace_invoice_payment_allocations(
				doc,
				list(doc.payments),
				[
					{"mode_of_payment": "Cash Ugx", "amount": 1_000_000},
					{"mode_of_payment": "MTN Mobile Money", "amount": 280_000},
				],
			)

		self.assertEqual(sum(row.amount for row in doc.payments), 1_280_000)
		self.assertEqual(doc.paid_amount, 1_280_000)
		self.assertEqual(
			{row.mode_of_payment: row.amount for row in doc.payments},
			{"Cash Ugx": 1_000_000, "MTN Mobile Money": 280_000},
		)
		self.assertEqual(
			set(details["target_accounts"]),
			{"1110 - Cash - 4S", "MTN Mobile Money - 4S"},
		)

	def test_payment_mode_split_rejects_a_changed_total(self):
		doc = _invoice()
		with (
			patch.object(correction, "_get_mode_of_payment", side_effect=lambda company, name: _mode(name)),
			patch.object(correction, "_validate_invoice_payment_account_currency"),
			self.assertRaises(frappe.ValidationError),
		):
			correction._replace_invoice_payment_allocations(
				doc,
				list(doc.payments),
				[
					{"mode_of_payment": "Cash Ugx", "amount": 1_000_000},
					{"mode_of_payment": "MTN Mobile Money", "amount": 279_999},
				],
				)

	def test_duplicate_allocations_that_collapse_to_the_existing_distribution_are_a_noop(self):
		doc = _invoice(paid_amount=300)
		with (
			patch.object(correction, "_get_mode_of_payment", side_effect=lambda company, name: _mode(name)),
			patch.object(correction, "_validate_invoice_payment_account_currency"),
			self.assertRaises(frappe.ValidationError),
		):
			correction._replace_invoice_payment_allocations(
				doc,
				list(doc.payments),
				[
					{"mode_of_payment": "Cash Ugx", "amount": 100},
					{"mode_of_payment": "Cash Ugx", "amount": 200},
				],
			)

	def test_duplicate_payment_allocations_are_combined_with_base_totals(self):
		doc = _invoice(paid_amount=300, conversion_rate=1.25)
		with (
			patch.object(correction, "_get_mode_of_payment", side_effect=lambda company, name: _mode(name)),
			patch.object(correction, "_validate_invoice_payment_account_currency"),
		):
			details = correction._replace_invoice_payment_allocations(
				doc,
				list(doc.payments),
				[
					{"mode_of_payment": "Cash Ugx", "amount": 100},
					{"mode_of_payment": "MTN Mobile Money", "amount": 50},
					{"mode_of_payment": "Cash Ugx", "amount": 150},
				],
			)

		self.assertEqual(len(doc.payments), 2)
		self.assertEqual(
			{row.mode_of_payment: (row.amount, row.base_amount) for row in doc.payments},
			{"Cash Ugx": (250, 312.5), "MTN Mobile Money": (50, 62.5)},
		)
		self.assertEqual([row.default for row in doc.payments], [1, 0])
		self.assertEqual(details["preserved_paid_amount"], 300)

	def test_return_payment_allocations_must_keep_the_negative_sign(self):
		doc = _invoice(paid_amount=-300)
		doc.payments[0].amount = -300
		doc.payments[0].base_amount = -300
		with (
			patch.object(correction, "_get_mode_of_payment", side_effect=lambda company, name: _mode(name)),
			patch.object(correction, "_validate_invoice_payment_account_currency"),
			self.assertRaises(frappe.ValidationError),
		):
			correction._replace_invoice_payment_allocations(
				doc,
				list(doc.payments),
				[
					{"mode_of_payment": "Cash Ugx", "amount": -200},
					{"mode_of_payment": "MTN Mobile Money", "amount": 100},
				],
			)

	def test_existing_payment_rows_must_match_paid_amount_before_redistribution(self):
		doc = _invoice(paid_amount=300)
		doc.payments[0].amount = 299
		with self.assertRaises(frappe.ValidationError):
			correction._replace_invoice_payment_allocations(
				doc,
				list(doc.payments),
				[{"mode_of_payment": "Cash Ugx", "amount": 300}],
			)

	def test_single_mode_change_reuses_matching_zero_value_row(self):
		doc = _invoice(paid_amount=300)
		doc.is_pos = 1
		source = doc.payments[0]
		source.default = 1
		source.get_db_value = lambda fieldname: "Cash Ugx" if fieldname == "mode_of_payment" else None
		target = frappe._dict(
			name="ROW-2",
			mode_of_payment="MTN Mobile Money",
			account="MTN Mobile Money - 4S",
			amount=0,
			base_amount=0,
			default=0,
		)
		doc.payments.append(target)

		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")),
			patch.object(correction, "_validate_same_account_currency"),
		):
			details = correction._change_invoice_mode_of_payment(doc, "ROW-1", "MTN Mobile Money")

		self.assertEqual((source.amount, source.base_amount, source.default), (0, 0, 0))
		self.assertEqual((target.amount, target.base_amount, target.default), (300, 300, 1))
		self.assertEqual(details["old_account"], "1110 - Cash - 4S")
		self.assertEqual(details["target_account"], "MTN Mobile Money - 4S")

	def test_invoice_allocation_rebuild_rejects_a_referenced_active_payment_row(self):
		doc = _invoice()
		doc.is_pos = 1
		doc.payments[0].reference_no = "MOMO-TXN-001"
		original_payments = copy.deepcopy(doc.payments)

		with (
			patch.object(correction, "_get_mode_of_payment", side_effect=lambda company, name: _mode(name)),
			patch.object(correction, "_validate_invoice_payment_account_currency"),
			patch.object(correction, "_validate_same_account_currency"),
			self.assertRaises(frappe.ValidationError),
		):
			correction._change_invoice_mode_of_payment(
				doc,
				None,
				None,
				[
					{"mode_of_payment": "Cash Ugx", "amount": 1_000_000},
					{"mode_of_payment": "MTN Mobile Money", "amount": 280_000},
				],
			)

		self.assertEqual(doc.payments, original_payments)

	def test_invoice_single_mode_change_rejects_reference_before_zero_target_reuse(self):
		doc = _invoice(paid_amount=300)
		doc.is_pos = 1
		source = doc.payments[0]
		source.default = 1
		source.reference_no = "CASH-RECEIPT-001"
		source.get_db_value = lambda fieldname: "Cash Ugx" if fieldname == "mode_of_payment" else None
		target = frappe._dict(
			name="ROW-2",
			mode_of_payment="MTN Mobile Money",
			account="MTN Mobile Money - 4S",
			amount=0,
			base_amount=0,
			default=0,
		)
		doc.payments.append(target)

		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")),
			patch.object(correction, "_validate_same_account_currency"),
			self.assertRaises(frappe.ValidationError),
		):
			correction._change_invoice_mode_of_payment(doc, source.name, "MTN Mobile Money")

		self.assertEqual((source.amount, source.base_amount, source.default), (300, 300, 1))
		self.assertEqual((target.amount, target.base_amount, target.default), (0, 0, 0))

	def test_include_payment_uses_outstanding_amount_and_conversion_rate(self):
		doc = _unpaid_invoice(outstanding_amount=250_000, conversion_rate=1.25)
		doc.payment_terms_template = "Net 30"
		doc.payment_schedule = [
			frappe._dict(
				idx=1,
				payment_term="30 Days",
				due_date=date(2026, 10, 17),
				invoice_portion=100,
				payment_amount=250_000,
				base_payment_amount=312_500,
			)
		]
		old_payment_schedule = correction._payment_schedule_state(doc)
		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")),
			patch.object(correction, "_validate_invoice_payment_account_currency"),
		):
			details = correction._include_invoice_payment(doc, "MTN Mobile Money")

		self.assertEqual(doc.is_pos, 1)
		self.assertEqual(len(doc.payments), 1)
		self.assertEqual(doc.payments[0].amount, 250_000)
		self.assertEqual(doc.payments[0].base_amount, 312_500)
		self.assertEqual(doc.conversion_rate, 1.25)
		self.assertEqual((doc.paid_amount, doc.base_paid_amount), (250_000, 312_500))
		self.assertEqual((doc.change_amount, doc.base_change_amount), (0, 0))
		self.assertEqual(doc.payment_terms_template, "")
		self.assertEqual(doc.payment_schedule, [])
		self.assertEqual(
			details,
			{
				"old_payment_terms_template": "Net 30",
				"old_payment_schedule": old_payment_schedule,
				"expected_payment_terms_template": "",
				"expected_payment_schedule": [],
				"new_mode_of_payment": "MTN Mobile Money",
				"target_account": "MTN Mobile Money - 4S",
				"party_account": "1310 - Debtors - 4S",
				"included_amount": 250_000,
				"base_included_amount": 312_500,
				"old_payment_allocations": [],
				"new_payment_allocations": [
					{
						"mode_of_payment": "MTN Mobile Money",
						"account": "MTN Mobile Money - 4S",
						"amount": 250_000,
						"base_amount": 312_500,
					}
				],
				"change_account": None,
				"old_change_amount": 0.0,
				"old_base_change_amount": 0.0,
				"new_change_amount": 0.0,
				"new_base_change_amount": 0.0,
			},
		)

	def test_include_payment_rejects_pos_or_fully_paid_invoice(self):
		for doc in (
			_unpaid_invoice(is_pos=1),
			_unpaid_invoice(outstanding_amount=0),
		):
			with self.subTest(is_pos=doc.is_pos, outstanding=doc.outstanding_amount):
				with self.assertRaises(frappe.ValidationError):
					correction._include_invoice_payment(doc, "Cash Ugx")

	def test_include_payment_converts_party_currency_outstanding_to_invoice_currency(self):
		doc = _unpaid_invoice(outstanding_amount=250_000, conversion_rate=1.25)
		doc.party_account_currency = "UGX"
		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")),
			patch.object(correction, "_validate_invoice_payment_account_currency"),
		):
			details = correction._include_invoice_payment(doc, "MTN Mobile Money")

		self.assertEqual(doc.payments[0].amount, 200_000)
		self.assertEqual(doc.payments[0].base_amount, 250_000)
		self.assertEqual((doc.paid_amount, doc.base_paid_amount), (200_000, 250_000))
		self.assertEqual(details["included_amount"], 200_000)
		self.assertEqual(details["base_included_amount"], 250_000)

	def test_include_payment_rejects_zero_conversion_rate_for_different_party_currency(self):
		doc = _unpaid_invoice(outstanding_amount=250_000, conversion_rate=0)
		doc.party_account_currency = "UGX"
		with self.assertRaises(frappe.ValidationError):
			correction._invoice_include_amount(doc)

	def test_include_payment_rejects_account_in_an_unrelated_currency(self):
		doc = _unpaid_invoice()
		currencies = {
			("Account", "MTN Mobile Money - 4S", "account_currency"): "EUR",
			("Company", correction.FOUR_S_COMPANY, "default_currency"): "UGX",
		}
		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")),
			patch.object(
				correction.frappe,
				"get_cached_value",
				side_effect=lambda doctype, name, fieldname: currencies[(doctype, name, fieldname)],
			),
			self.assertRaises(frappe.ValidationError),
		):
			correction._include_invoice_payment(doc, "MTN Mobile Money")

		self.assertEqual(doc.is_pos, 0)
		self.assertEqual(doc.payments, [])

	def test_exclude_payment_clears_rows_and_all_payment_totals(self):
		doc = _invoice(paid_amount=300)
		doc.is_pos = 1
		doc.change_amount = 20
		doc.base_change_amount = 20

		details = correction._exclude_invoice_payment(doc)

		self.assertEqual(doc.is_pos, 0)
		self.assertEqual(doc.payments, [])
		self.assertEqual(
			(doc.paid_amount, doc.base_paid_amount, doc.change_amount, doc.base_change_amount),
			(0, 0, 0, 0),
		)
		self.assertEqual(details["old_payment_accounts"], ["1110 - Cash - 4S"])
		self.assertEqual(doc.set_payment_schedule_calls, 1)
		self.assertEqual(doc.payment_terms_template, "")
		self.assertEqual(len(doc.payment_schedule), 1)
		self.assertEqual(details["expected_payment_schedule"], correction._payment_schedule_state(doc))

	def test_exclude_payment_rejects_non_pos_invoice(self):
		doc = _invoice()
		doc.is_pos = 0
		with self.assertRaises(frappe.ValidationError):
			correction._exclude_invoice_payment(doc)

	def test_payment_schedule_transition_is_verified_exactly(self):
		doc = _invoice(paid_amount=300)
		doc.is_pos = 1
		details = correction._exclude_invoice_payment(doc)

		correction._verify_invoice_payment_schedule_transition(
			doc, correction.EXCLUDE_PAYMENT, details
		)
		doc.payment_schedule[0].due_date = date(2026, 10, 1)
		with self.assertRaises(frappe.ValidationError):
			correction._verify_invoice_payment_schedule_transition(
				doc, correction.EXCLUDE_PAYMENT, details
			)

	def test_payment_schedule_state_normalizes_discount_type_default(self):
		doc = _invoice()
		doc.payment_schedule = [frappe._dict(idx=1, discount_type=None)]
		before_insert = correction._payment_schedule_state(doc)
		doc.payment_schedule[0].discount_type = "Percentage"

		self.assertEqual(before_insert, correction._payment_schedule_state(doc))

	def test_include_payment_schedule_must_remain_empty(self):
		doc = _unpaid_invoice()
		details = {
			"expected_payment_terms_template": "",
			"expected_payment_schedule": [],
		}
		correction._verify_invoice_payment_schedule_transition(
			doc, correction.INCLUDE_PAYMENT, details
		)

		doc.payment_terms_template = "Net 30"
		with self.assertRaises(frappe.ValidationError):
			correction._verify_invoice_payment_schedule_transition(
				doc, correction.INCLUDE_PAYMENT, details
			)

	def test_payment_entry_mode_change_updates_the_correct_account_side_only(self):
		for payment_type, changed_field, unchanged_field in (
			("Receive", "paid_to", "paid_from"),
			("Pay", "paid_from", "paid_to"),
		):
			with self.subTest(payment_type=payment_type):
				doc = _payment_entry(payment_type=payment_type)
				unchanged_account = doc.get(unchanged_field)
				amounts_before = {
					fieldname: doc.get(fieldname) for fieldname in correction.PAYMENT_ENTRY_AMOUNT_FIELDS
				}
				with (
					patch.object(correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")),
					patch.object(correction, "_validate_same_account_currency"),
				):
					details = correction._change_payment_entry_mode_of_payment(
						doc, "MTN Mobile Money", None, None, None
					)

				self.assertEqual(doc.get(changed_field), "MTN Mobile Money - 4S")
				self.assertEqual(doc.get(unchanged_field), unchanged_account)
				self.assertEqual(doc.get(f"{changed_field}_account_currency"), "UGX")
				self.assertEqual(doc.get(f"{changed_field}_account_type"), "Cash")
				self.assertEqual(details["account_side"], changed_field)
				self.assertEqual(
					{fieldname: doc.get(fieldname) for fieldname in correction.PAYMENT_ENTRY_AMOUNT_FIELDS},
					amounts_before,
				)

	def test_internal_transfer_mode_always_targets_paid_from(self):
		doc = _payment_entry(payment_type="Internal Transfer")
		self.assertEqual(correction._payment_entry_account_field(doc, None), "paid_from")
		self.assertEqual(correction._payment_entry_account_field(doc, "paid_from"), "paid_from")
		for account_side in ("account", "paid_from_and_to", "paid_to"):
			with self.subTest(account_side=account_side), self.assertRaises(frappe.ValidationError):
				correction._payment_entry_account_field(doc, account_side)

	def test_bank_payment_requires_reference_number_and_date(self):
		for reference_no, reference_date in ((None, None), ("BANK-REF", None), (None, "2026-09-17")):
			with self.subTest(reference_no=reference_no, reference_date=reference_date):
				doc = _payment_entry()
				doc.reference_no = None
				doc.reference_date = None
				with (
					patch.object(correction, "_get_mode_of_payment", return_value=_mode("Bank Transfer")),
					patch.object(correction, "_validate_same_account_currency"),
					self.assertRaises(frappe.ValidationError),
				):
					correction._change_payment_entry_mode_of_payment(
						doc, "Bank Transfer", None, reference_no, reference_date
					)

	def test_bank_payment_accepts_new_reference_values(self):
		doc = _payment_entry()
		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("Bank Transfer")),
			patch.object(correction, "_validate_same_account_currency"),
			patch.object(
				correction.frappe.db,
				"get_all",
				return_value=[
					frappe._dict(
						name="BANK-ACCOUNT-1",
						bank="New Bank",
						bank_account_no="NEW-001",
						is_default=1,
					)
				],
			),
		):
			details = correction._change_payment_entry_mode_of_payment(
				doc, "Bank Transfer", None, " BANK-2026 ", "2026-09-17"
			)

		self.assertEqual(doc.reference_no, "BANK-2026")
		self.assertEqual(doc.reference_date, date(2026, 9, 17))
		self.assertEqual(doc.bank_account, "BANK-ACCOUNT-1")
		self.assertEqual(doc.bank, "New Bank")
		self.assertEqual(doc.bank_account_no, "NEW-001")
		self.assertEqual(details["reference_no"], "BANK-2026")
		self.assertTrue(details["bank_metadata_synced"])

	def test_payment_entry_mode_change_regenerates_automatic_remarks_after_reference_change(self):
		doc = _payment_entry()
		doc.custom_remarks = 0
		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("Bank Transfer")),
			patch.object(correction, "_validate_same_account_currency"),
			patch.object(
				correction.frappe.db,
				"get_all",
				return_value=[frappe._dict(name="BANK-ACCOUNT-1", is_default=1)],
			),
		):
			correction._change_payment_entry_mode_of_payment(
				doc, "Bank Transfer", None, "BANK-2026", "2026-09-17"
			)

		self.assertEqual(doc.set_remarks_calls, 1)
		self.assertIn("Bank Transfer", doc.remarks)
		self.assertIn("BANK-2026", doc.remarks)

	def test_payment_entry_mode_change_preserves_custom_remarks(self):
		doc = _payment_entry()
		doc.custom_remarks = 1
		with (
			patch.object(
				correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")
			),
			patch.object(correction, "_validate_same_account_currency"),
			patch.object(correction.frappe.db, "get_all", return_value=[]),
		):
			correction._change_payment_entry_mode_of_payment(
				doc, "MTN Mobile Money", None, None, None
			)

		self.assertEqual(doc.remarks, "Existing remarks")
		self.assertEqual(getattr(doc, "set_remarks_calls", 0), 0)

	def test_internal_transfer_mode_change_regenerates_title(self):
		doc = _payment_entry(payment_type="Internal Transfer")
		doc.custom_remarks = 0
		doc.bank_account = None
		doc.bank = None
		doc.bank_account_no = None
		with (
			patch.object(
				correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")
			),
			patch.object(correction, "_validate_same_account_currency"),
		):
			correction._change_payment_entry_mode_of_payment(
				doc, "MTN Mobile Money", "paid_from", None, None
			)

		self.assertEqual(doc.set_remarks_calls, 1)
		self.assertEqual(doc.set_title_calls, 1)
		self.assertEqual(doc.title, "MTN Mobile Money - 4S - Creditors - 4S")

	def test_internal_transfer_bank_reference_follows_paid_from(self):
		doc = _payment_entry(payment_type="Internal Transfer")
		doc.bank_account = None
		doc.bank = None
		doc.bank_account_no = None
		doc.reference_no = None
		doc.reference_date = None
		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("Bank Transfer")),
			patch.object(correction, "_validate_same_account_currency"),
			self.assertRaises(frappe.ValidationError),
		):
			correction._change_payment_entry_mode_of_payment(
				doc, "Bank Transfer", "paid_from", None, None
			)

		doc = _payment_entry(payment_type="Internal Transfer")
		doc.bank_account = None
		doc.bank = None
		doc.bank_account_no = None
		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")),
			patch.object(correction, "_validate_same_account_currency"),
		):
			correction._change_payment_entry_mode_of_payment(
				doc, "MTN Mobile Money", "paid_from", None, None
			)
		self.assertIsNone(doc.reference_no)
		self.assertIsNone(doc.reference_date)

	def test_payment_entry_mode_change_clears_stale_unmapped_bank_metadata(self):
		doc = _payment_entry()
		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")),
			patch.object(correction, "_validate_same_account_currency"),
			patch.object(correction.frappe.db, "get_all", return_value=[]),
		):
			details = correction._change_payment_entry_mode_of_payment(
				doc, "MTN Mobile Money", None, None, None
			)

		self.assertIsNone(doc.bank_account)
		self.assertIsNone(doc.bank)
		self.assertIsNone(doc.bank_account_no)
		self.assertIsNone(details["bank_account"])

	def test_ambiguous_company_bank_account_mapping_is_rejected(self):
		doc = _payment_entry()
		rows = [
			frappe._dict(name="BANK-1", is_default=0),
			frappe._dict(name="BANK-2", is_default=0),
		]
		with (
			patch.object(correction.frappe.db, "get_all", return_value=rows),
			self.assertRaises(frappe.ValidationError),
		):
			correction._sync_company_bank_account_metadata(doc, "Bank - 4S")

	def test_internal_transfer_rejects_ambiguous_bank_metadata(self):
		doc = _payment_entry(payment_type="Internal Transfer")
		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")),
			patch.object(correction, "_validate_same_account_currency"),
			self.assertRaises(frappe.ValidationError),
		):
			correction._change_payment_entry_mode_of_payment(
				doc, "MTN Mobile Money", "paid_from", None, None
			)

	def test_payment_entry_verification_treats_null_and_empty_bank_metadata_as_equal(self):
		doc = _payment_entry()
		doc.docstatus = 1
		doc.mode_of_payment = "Bank Transfer"
		doc.paid_to = "Bank - 4S"
		doc.paid_to_account_type = "Bank"
		doc.bank_account = "COMPANY-BANK"
		doc.bank = "Centenary"
		doc.bank_account_no = ""
		details = {
			"account_side": "paid_to",
			"new_mode_of_payment": "Bank Transfer",
			"old_account": "1110 - Cash - 4S",
			"target_account": "Bank - 4S",
			"target_account_currency": "UGX",
			"target_account_type": "Bank",
			"reference_no": "OLD-REF",
			"reference_date": date(2026, 9, 1),
			"bank_account": "COMPANY-BANK",
			"bank": "Centenary",
			"bank_account_no": None,
		}

		with patch.object(correction, "_verify_active_gl_matches_document"):
			correction._verify_result(
				doc,
				correction.CHANGE_MODE_OF_PAYMENT,
				details,
				_ledger_snapshot(),
				_ledger_snapshot(),
			)

	def test_non_bank_mode_clears_stale_bank_reference_values(self):
		doc = _payment_entry()
		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")),
			patch.object(correction, "_validate_same_account_currency"),
		):
			correction._change_payment_entry_mode_of_payment(
				doc, "MTN Mobile Money", None, "IGNORED", "2026-09-17"
			)

		self.assertIsNone(doc.reference_no)
		self.assertIsNone(doc.reference_date)

	def test_payment_entry_mode_change_rejects_account_currency_mismatch(self):
		doc = _payment_entry()
		currencies = {"1110 - Cash - 4S": "UGX", "MTN Mobile Money - 4S": "USD"}
		with (
			patch.object(correction, "_get_mode_of_payment", return_value=_mode("MTN Mobile Money")),
			patch.object(
				correction.frappe,
				"get_cached_value",
				side_effect=lambda doctype, name, fieldname: currencies[name],
			),
			self.assertRaises(frappe.ValidationError),
		):
			correction._change_payment_entry_mode_of_payment(doc, "MTN Mobile Money", None, None, None)

		self.assertEqual(doc.mode_of_payment, "Cash Ugx")
		self.assertEqual(doc.paid_to, "1110 - Cash - 4S")

	def test_posting_date_change_sets_sales_invoice_posting_time_flag_only(self):
		for doctype in ("Sales Invoice", "Payment Entry"):
			with self.subTest(doctype=doctype):
				doc = _Invoice(
					doctype=doctype,
					company=correction.FOUR_S_COMPANY,
					posting_date=date(2026, 9, 16),
					set_posting_time=0,
				)
				with patch.object(correction, "_validate_open_accounting_dates") as validate_dates:
					details = correction._change_posting_date(doc, "2026-09-17")

				self.assertEqual(doc.posting_date, date(2026, 9, 17))
				self.assertEqual(doc.set_posting_time, 1 if doctype == "Sales Invoice" else 0)
				self.assertEqual(details["old_posting_date"], date(2026, 9, 16))
				validate_dates.assert_called_once_with(doc, date(2026, 9, 16), date(2026, 9, 17))

	def test_posting_date_change_rejects_same_date_without_period_checks(self):
		doc = _Invoice(
			doctype="Sales Invoice",
			company=correction.FOUR_S_COMPANY,
			posting_date=date(2026, 9, 17),
		)
		with (
			patch.object(correction, "_validate_open_accounting_dates") as validate_dates,
			self.assertRaises(frappe.ValidationError),
		):
			correction._change_posting_date(doc, "2026-09-17")
		validate_dates.assert_not_called()

	def test_old_and_new_posting_dates_must_both_be_in_open_periods(self):
		doc = _Invoice(
			doctype="Payment Entry",
			company=correction.FOUR_S_COMPANY,
			is_opening="No",
		)
		with (
			patch("erpnext.accounts.general_ledger.check_freezing_date") as check_freezing,
			patch("erpnext.accounts.general_ledger.validate_against_pcv") as validate_pcv,
			patch("erpnext.accounts.general_ledger.validate_accounting_period") as validate_period,
		):
			correction._validate_open_accounting_dates(doc, date(2026, 9, 16), date(2026, 9, 17))

		check_freezing.assert_has_calls(
			[
				call(date(2026, 9, 16), correction.FOUR_S_COMPANY),
				call(date(2026, 9, 17), correction.FOUR_S_COMPANY),
			],
			any_order=True,
		)
		validate_pcv.assert_has_calls(
			[
				call(False, date(2026, 9, 16), correction.FOUR_S_COMPANY),
				call(False, date(2026, 9, 17), correction.FOUR_S_COMPANY),
			],
			any_order=True,
		)
		self.assertEqual(validate_period.call_count, 2)
		self.assertEqual(
			{period_call.args[0][0].posting_date for period_call in validate_period.call_args_list},
			{date(2026, 9, 16), date(2026, 9, 17)},
		)

	def test_date_verification_accepts_date_derived_ledger_changes_after_exact_replay(self):
		before_gl = [
			frappe._dict(
				posting_date=date(2026, 9, 16),
				fiscal_year="2025",
				reporting_currency_exchange_rate=2,
				debit_in_reporting_currency=200,
				credit_in_reporting_currency=0,
				account="Cash",
				debit=100,
				credit=0,
			),
			frappe._dict(
				posting_date=date(2026, 9, 16),
				fiscal_year="2025",
				reporting_currency_exchange_rate=2,
				debit_in_reporting_currency=0,
				credit_in_reporting_currency=200,
				account="Sales",
				debit=0,
				credit=100,
			),
		]
		after_gl = [
			frappe._dict(
				posting_date=date(2026, 9, 17),
				fiscal_year="2026",
				reporting_currency_exchange_rate=3,
				debit_in_reporting_currency=300,
				credit_in_reporting_currency=0,
				account="Cash",
				debit=100,
				credit=0,
			),
			frappe._dict(
				posting_date=date(2026, 9, 17),
				fiscal_year="2026",
				reporting_currency_exchange_rate=3,
				debit_in_reporting_currency=0,
				credit_in_reporting_currency=300,
				account="Sales",
				debit=0,
				credit=100,
			),
		]
		before = _ledger_snapshot(gl_entries=before_gl)
		after = _ledger_snapshot(gl_entries=after_gl)
		doc = _PaymentEntry(
			doctype="Payment Entry", docstatus=1, posting_date=date(2026, 9, 17)
		)

		with patch.object(correction, "_verify_active_gl_matches_document") as verify_exact_gl:
			correction._verify_result(
				doc,
				correction.CHANGE_POSTING_DATE,
				{"new_posting_date": date(2026, 9, 17)},
				before,
				after,
			)

		verify_exact_gl.assert_called_once_with(doc, after)

	def test_payment_entry_date_verification_accepts_reference_effect_dates(self):
		new_date = date(2026, 9, 17)
		reconciliation_date = date(2026, 9, 12)
		doc = _PaymentEntry(
			doctype="Payment Entry",
			docstatus=1,
			posting_date=new_date,
			book_advance_payments_in_separate_party_account=1,
			references=[
				frappe._dict(
					name="REF-1",
					reference_doctype="Sales Invoice",
					reference_name="SI-1",
					reconcile_effect_on=reconciliation_date,
				)
			],
		)
		after = _ledger_snapshot(
			gl_entries=[
				frappe._dict(posting_date=new_date, debit=100, credit=0),
				frappe._dict(posting_date=reconciliation_date, debit=0, credit=100),
			],
			payment_entries=[
				frappe._dict(posting_date=new_date),
				frappe._dict(posting_date=reconciliation_date),
			],
		)

		with patch.object(correction, "_verify_active_gl_matches_document"):
			correction._verify_result(
				doc,
				correction.CHANGE_POSTING_DATE,
				{"new_posting_date": new_date},
				_ledger_snapshot(),
				after,
			)

	def test_payment_entry_date_verification_rejects_unrelated_ledger_date(self):
		new_date = date(2026, 9, 17)
		doc = _PaymentEntry(
			doctype="Payment Entry",
			docstatus=1,
			posting_date=new_date,
			book_advance_payments_in_separate_party_account=1,
			references=[
				frappe._dict(
					name="REF-1",
					reference_doctype="Sales Invoice",
					reference_name="SI-1",
					reconcile_effect_on=date(2026, 9, 12),
				)
			],
		)
		after = _ledger_snapshot(
			gl_entries=[
				frappe._dict(posting_date=new_date, debit=100, credit=0),
				frappe._dict(posting_date=date(2026, 9, 11), debit=0, credit=100),
			]
		)

		with (
			patch.object(correction, "_verify_active_gl_matches_document"),
			self.assertRaises(frappe.ValidationError),
		):
			correction._verify_result(
				doc,
				correction.CHANGE_POSTING_DATE,
				{"new_posting_date": new_date},
				_ledger_snapshot(),
				after,
			)

	def test_missing_reference_effect_date_is_calculated_without_mutating_document(self):
		reference = frappe._dict(
			name="REF-1",
			reference_doctype="Sales Invoice",
			reference_name="SI-1",
			reconcile_effect_on=None,
		)
		doc = _PaymentEntry(
			doctype="Payment Entry",
			company=correction.FOUR_S_COMPANY,
			posting_date=date(2026, 9, 17),
			book_advance_payments_in_separate_party_account=1,
			references=[reference],
		)
		with patch(
			"erpnext.accounts.utils.get_reconciliation_effect_date",
			return_value=date(2026, 9, 12),
		):
			dates = correction._payment_entry_ledger_posting_dates(doc)

		self.assertEqual(dates, {date(2026, 9, 17), date(2026, 9, 12)})
		self.assertIsNone(reference.reconcile_effect_on)

	def test_repost_temporarily_seeds_and_restores_missing_reference_effect_date(self):
		reference = frappe._dict(
			name="REF-1",
			reference_doctype="Sales Invoice",
			reference_name="SI-1",
			reconcile_effect_on=None,
		)
		doc = _PaymentEntry(
			doctype="Payment Entry",
			company=correction.FOUR_S_COMPANY,
			posting_date=date(2026, 9, 17),
			book_advance_payments_in_separate_party_account=1,
			references=[reference],
		)
		with (
			patch(
				"erpnext.accounts.utils.get_reconciliation_effect_date",
				return_value=date(2026, 9, 12),
			),
			patch.object(correction.frappe.db, "set_value") as set_value,
			self.assertRaisesRegex(RuntimeError, "repost failed"),
		):
			with correction._temporary_payment_entry_reconciliation_dates(doc):
				raise RuntimeError("repost failed")

		self.assertEqual(
			set_value.call_args_list,
			[
				call(
					"Payment Entry Reference",
					"REF-1",
					"reconcile_effect_on",
					date(2026, 9, 12),
					update_modified=False,
				),
				call(
					"Payment Entry Reference",
					"REF-1",
					"reconcile_effect_on",
					None,
					update_modified=False,
				),
			],
		)
		self.assertIsNone(reference.reconcile_effect_on)

	def test_stock_backed_invoice_date_change_is_rejected_before_period_checks(self):
		doc = _Invoice(
			doctype="Sales Invoice",
			update_stock=1,
			posting_date=date(2026, 9, 16),
		)
		with (
			patch.object(correction, "_validate_open_accounting_dates") as validate_dates,
			self.assertRaises(frappe.ValidationError),
		):
			correction._change_posting_date(doc, "2026-09-17")
		validate_dates.assert_not_called()

	def test_invoice_action_availability_tracks_pos_state_outstanding_and_used_action(self):
		base_values = {
			"doctype": "Sales Invoice",
			"name": "TEST-SI",
			"company": correction.FOUR_S_COMPANY,
			"posting_date": date(2026, 9, 17),
			"modified": "2026-09-17 10:00:00",
			"mode_of_payment": None,
			"payment_type": None,
			"paid_from": None,
			"paid_to": None,
			"reference_no": None,
			"reference_date": None,
			"currency": "UGX",
			"party_account_currency": "UGX",
			"conversion_rate": 1,
		}
		cases = [
			(
				_Invoice(**base_values, is_pos=0, outstanding_amount=100, payments=[]),
				{},
				{
					correction.INCLUDE_PAYMENT: True,
					correction.EXCLUDE_PAYMENT: False,
					correction.CHANGE_MODE_OF_PAYMENT: False,
				},
			),
			(
				_Invoice(**base_values, is_pos=0, outstanding_amount=0, payments=[]),
				{},
				{
					correction.INCLUDE_PAYMENT: False,
					correction.EXCLUDE_PAYMENT: False,
					correction.CHANGE_MODE_OF_PAYMENT: False,
				},
			),
			(
				_Invoice(
					**base_values,
					is_pos=1,
					outstanding_amount=0,
					payments=[
						frappe._dict(
							name="ROW-1",
							mode_of_payment="Cash Ugx",
							account="1110 - Cash - 4S",
							amount=100,
							base_amount=100,
						)
					],
				),
				{correction.EXCLUDE_PAYMENT: {"name": "AUDIT-1"}},
				{
					correction.INCLUDE_PAYMENT: False,
					correction.EXCLUDE_PAYMENT: False,
					correction.CHANGE_MODE_OF_PAYMENT: True,
				},
			),
		]

		for doc, used_actions, expected in cases:
			with (
				self.subTest(is_pos=doc.is_pos, outstanding=doc.outstanding_amount, used=used_actions),
				patch.object(correction, "_get_authorized_document", return_value=doc),
				patch.object(correction, "_used_actions", return_value=used_actions),
				patch.object(correction, "_mode_of_payment_choices", return_value=[]),
			):
				context = correction.get_correction_context("Sales Invoice", "TEST-SI")
				for action, available in expected.items():
					self.assertEqual(context["available_actions"][action], available)

	def test_stock_backed_invoice_date_action_is_not_offered(self):
		doc = _Invoice(
			doctype="Sales Invoice",
			name="TEST-STOCK-SI",
			company=correction.FOUR_S_COMPANY,
			posting_date=date(2026, 9, 17),
			modified="2026-09-17 10:00:00",
			is_pos=0,
			update_stock=1,
			outstanding_amount=0,
			payments=[],
			currency="UGX",
			party_account_currency="UGX",
			conversion_rate=1,
			mode_of_payment=None,
			payment_type=None,
			paid_from=None,
			paid_to=None,
			reference_no=None,
			reference_date=None,
		)
		with (
			patch.object(correction, "_get_authorized_document", return_value=doc),
			patch.object(correction, "_used_actions", return_value={}),
			patch.object(correction, "_mode_of_payment_choices", return_value=[]),
		):
			context = correction.get_correction_context("Sales Invoice", doc.name)

		self.assertFalse(context["available_actions"][correction.CHANGE_POSTING_DATE])

	def test_each_action_has_a_distinct_one_time_key(self):
		keys = {
			correction._action_key("Sales Invoice", "TEST-SI", action)
			for action in correction.DOCTYPE_ACTIONS["Sales Invoice"]
		}
		self.assertEqual(len(keys), 4)
		self.assertEqual(len(next(iter(keys))), 64)

	def test_payment_entry_context_uses_the_active_account_side_currency(self):
		doc = _payment_entry(payment_type="Pay")
		doc.docstatus = 1
		doc.modified = "2026-09-17 10:00:00"
		doc.paid_from_account_currency = "USD"
		doc.paid_to_account_currency = "UGX"
		with (
			patch.object(correction, "_get_authorized_document", return_value=doc),
			patch.object(correction, "_used_actions", return_value={}),
			patch.object(correction, "_mode_of_payment_choices", return_value=[]),
		):
			context = correction.get_correction_context("Payment Entry", doc.name)

		self.assertEqual(context["currency"], "USD")

	def test_stale_dialog_is_rejected_before_any_write(self):
		doc = SimpleNamespace(modified="2026-09-17 10:00:00")
		with (
			patch.object(correction, "_get_authorized_document", return_value=doc),
			patch.object(correction, "_apply_requested_change") as apply_change,
			self.assertRaises(frappe.ValidationError),
		):
			correction.apply_correction(
				"Sales Invoice",
				"TEST-SI",
				correction.CHANGE_POSTING_DATE,
				"Correcting an entered date",
				expected_modified="2026-09-17 09:59:59",
				new_posting_date="2026-09-16",
			)
		apply_change.assert_not_called()

	def test_only_4s_accountant_can_open_correction_context(self):
		with (
			patch.object(correction.frappe, "get_roles", return_value=["Accounts User"]),
			self.assertRaises(frappe.PermissionError),
		):
			correction._get_authorized_document("Sales Invoice", "TEST-SI")

	def test_amount_guard_allows_only_include_exclude_to_change_paid_fields(self):
		before = {
			field: 0.0 for field in (*correction.SALES_TOTAL_FIELDS, *correction.SALES_PAYMENT_TOTAL_FIELDS)
		}
		doc = _Invoice(doctype="Sales Invoice", **before)
		doc.paid_amount = 100
		correction._validate_protected_amounts(doc, before, correction.INCLUDE_PAYMENT)
		with self.assertRaises(frappe.ValidationError):
			correction._validate_protected_amounts(doc, before, correction.CHANGE_MODE_OF_PAYMENT)

	def test_structural_guard_allows_requested_date_only(self):
		old = _StateDoc(
			doctype="Sales Invoice",
			state={
				"doctype": "Sales Invoice",
				"name": "TEST-SI",
				"posting_date": "2026-09-16",
				"set_posting_time": 0,
				"customer": "Customer A",
				"items": [{"name": "ROW-1", "item_code": "ITEM", "qty": 1}],
			},
		)
		new = _StateDoc(
			doctype="Sales Invoice",
			state={
				**old.state,
				"posting_date": "2026-09-17",
				"set_posting_time": 1,
			},
		)
		correction._validate_protected_document(
			old,
			new,
			correction.CHANGE_POSTING_DATE,
			{"new_posting_date": "2026-09-17"},
		)
		new.state["customer"] = "Customer B"
		with self.assertRaises(frappe.ValidationError):
			correction._validate_protected_document(
				old,
				new,
				correction.CHANGE_POSTING_DATE,
				{"new_posting_date": "2026-09-17"},
			)

	def test_ledger_guard_ignores_only_declared_fields(self):
		before = [frappe._dict(posting_date="2026-09-16", account="Cash", debit=100)]
		after = [frappe._dict(posting_date="2026-09-17", account="Cash", debit=100)]
		correction._assert_ledger_rows_equal_except(before, after, {"posting_date"}, "GL")
		after[0].debit = 99
		with self.assertRaises(frappe.ValidationError):
			correction._assert_ledger_rows_equal_except(before, after, {"posting_date"}, "GL")

	def test_exact_document_ledger_verifier_rejects_row_dimension_and_currency_corruption(self):
		doc = _Invoice(doctype="Sales Invoice", name="TEST-SI")
		gl_row = frappe._dict(
			docstatus=1,
			account="1110 - Cash - 4S",
			party_type=None,
			party=None,
			cost_center="Main - 4S",
			branch="Kampala",
			debit=100,
			credit=0,
			debit_in_reporting_currency=100,
			credit_in_reporting_currency=0,
			reporting_currency_exchange_rate=1,
		)
		ple_row = frappe._dict(
			docstatus=1,
			company=correction.FOUR_S_COMPANY,
			account="1310 - Debtors - 4S",
			account_type="Receivable",
			party_type="Customer",
			party="Customer A",
			voucher_type="Sales Invoice",
			voucher_no="TEST-SI",
			amount=100,
			amount_in_account_currency=100,
			branch="Kampala",
		)
		aple_row = frappe._dict(
			docstatus=1,
			company=correction.FOUR_S_COMPANY,
			voucher_type="Sales Invoice",
			voucher_no="TEST-SI",
			against_voucher_type="Sales Invoice",
			against_voucher_no="TEST-SI",
			amount=100,
			base_amount=100,
			currency="UGX",
			exchange_rate=1,
			event="Submit",
		)
		clean = _ledger_snapshot(
			gl_entries=[gl_row], payment_entries=[ple_row], advance_entries=[aple_row]
		)
		clean["accounting_dimensions"] = ["branch"]

		with (
			patch.object(correction, "_expected_active_gl_entries", return_value=[gl_row]),
			patch.object(
				correction,
				"_expected_active_payment_ledgers",
				return_value=([ple_row], [aple_row]),
			),
		):
			correction._verify_active_gl_matches_document(doc, clean)

			corruptions = []
			wrong_dimension = copy.deepcopy(clean)
			wrong_dimension["gl_entries"][0].branch = "Entebbe"
			corruptions.append(wrong_dimension)
			wrong_reporting = copy.deepcopy(clean)
			wrong_reporting["gl_entries"][0].debit_in_reporting_currency = 99
			corruptions.append(wrong_reporting)
			duplicate_gl = copy.deepcopy(clean)
			duplicate_gl["gl_entries"].append(copy.deepcopy(gl_row))
			corruptions.append(duplicate_gl)
			wrong_docstatus = copy.deepcopy(clean)
			wrong_docstatus["gl_entries"][0].docstatus = 0
			corruptions.append(wrong_docstatus)
			split_ple = copy.deepcopy(clean)
			split_ple["payment_ledger_entries"] = [
				frappe._dict({**dict(ple_row), "amount": 40, "amount_in_account_currency": 40}),
				frappe._dict({**dict(ple_row), "amount": 60, "amount_in_account_currency": 60}),
			]
			corruptions.append(split_ple)
			split_aple = copy.deepcopy(clean)
			split_aple["advance_payment_ledger_entries"] = [
				frappe._dict({**dict(aple_row), "amount": 50, "base_amount": 50}),
				frappe._dict({**dict(aple_row), "amount": 50, "base_amount": 50}),
			]
			corruptions.append(split_aple)

			for corrupt in corruptions:
				with self.assertRaises(frappe.ValidationError):
					correction._verify_active_gl_matches_document(doc, corrupt)

	def test_ledger_snapshot_requests_enabled_accounting_dimensions(self):
		doc = _Invoice(doctype="Sales Invoice", name="TEST-SI", update_stock=0)
		with (
			patch(
				"erpnext.accounts.doctype.accounting_dimension.accounting_dimension.get_accounting_dimensions",
				return_value=["branch"],
			),
			patch.object(correction.frappe.db, "get_all", side_effect=[[], [], []]) as get_all,
		):
			snapshot = correction._ledger_snapshot(doc)

		self.assertIn("branch", get_all.call_args_list[0].kwargs["fields"])
		self.assertIn("branch", get_all.call_args_list[1].kwargs["fields"])
		self.assertEqual(snapshot["accounting_dimensions"], ["branch"])

	def test_mode_change_accepts_the_exact_requested_payment_distribution(self):
		doc = _invoice(paid_amount=100)
		doc.payments = [
			frappe._dict(
				mode_of_payment="MTN Mobile Money",
				account="MTN Mobile Money - 4S",
				amount=100,
				base_amount=100,
			)
		]
		details = {
			"old_payment_allocations": [
				{
					"mode_of_payment": "Cash Ugx",
					"account": "1110 - Cash - 4S",
					"amount": 100,
					"base_amount": 100,
				}
			],
			"new_payment_allocations": [
				{
					"mode_of_payment": "MTN Mobile Money",
					"account": "MTN Mobile Money - 4S",
					"amount": 100,
					"base_amount": 100,
				}
			],
			"preserved_paid_amount": 100,
			"preserved_base_paid_amount": 100,
		}

		correction._verify_invoice_payment_distribution(doc, details)

	def test_excluding_payment_leaves_an_exact_empty_payment_distribution(self):
		doc = _invoice(paid_amount=100)
		doc.is_pos = 1
		doc.change_amount = 20
		doc.base_change_amount = 20
		details = correction._exclude_invoice_payment(doc)

		correction._verify_invoice_payment_distribution(doc, details)

	def test_payment_distribution_rejects_a_corrupt_active_payment_row(self):
		doc = _invoice(paid_amount=100)
		doc.payments = [
			frappe._dict(
				mode_of_payment="MTN Mobile Money",
				account="MTN Mobile Money - 4S",
				amount=90,
				base_amount=90,
			)
		]
		details = {
			"old_payment_allocations": [
				{
					"mode_of_payment": "Cash Ugx",
					"account": "1110 - Cash - 4S",
					"amount": 100,
					"base_amount": 100,
				}
			],
			"new_payment_allocations": [
				{
					"mode_of_payment": "MTN Mobile Money",
					"account": "MTN Mobile Money - 4S",
					"amount": 100,
					"base_amount": 100,
				}
			],
			"preserved_paid_amount": 100,
			"preserved_base_paid_amount": 100,
		}

		with self.assertRaises(frappe.ValidationError):
			correction._verify_invoice_payment_distribution(doc, details)

	def test_payment_distribution_rejects_duplicate_active_rows_even_when_totals_match(self):
		doc = _invoice(paid_amount=300)
		doc.payments = [
			frappe._dict(
				mode_of_payment="Cash Ugx",
				account="1110 - Cash - 4S",
				amount=100,
				base_amount=100,
			),
			frappe._dict(
				mode_of_payment="Cash Ugx",
				account="1110 - Cash - 4S",
				amount=200,
				base_amount=200,
			),
		]
		details = {
			"new_payment_allocations": [
				{
					"mode_of_payment": "Cash Ugx",
					"account": "1110 - Cash - 4S",
					"amount": 300,
					"base_amount": 300,
				}
			],
			"preserved_paid_amount": 300,
			"preserved_base_paid_amount": 300,
		}

		with self.assertRaises(frappe.ValidationError):
			correction._verify_invoice_payment_distribution(doc, details)

	def test_fx_split_preserves_the_base_rounding_residual_exactly(self):
		doc = _invoice(paid_amount=3, conversion_rate=4 / 3)
		doc.currency = "USD"
		with (
			patch.object(correction, "_get_mode_of_payment", side_effect=lambda company, name: _mode(name)),
			patch.object(correction, "_validate_invoice_payment_account_currency"),
			patch.object(correction, "_validate_same_account_currency"),
		):
			details = correction._replace_invoice_payment_allocations(
				doc,
				list(doc.payments),
				[
					{"mode_of_payment": "Cash Ugx", "amount": 1},
					{"mode_of_payment": "MTN Mobile Money", "amount": 2},
				],
			)

		self.assertEqual([row.base_amount for row in doc.payments], [1.33, 2.67])
		correction._verify_invoice_payment_distribution(doc, details)

	def test_audit_doctype_is_read_only_and_one_time_key_is_unique(self):
		path = (
			PACKAGE_ROOT
			/ "fours_customizations"
			/ "doctype"
			/ "four_s_accounting_correction"
			/ "four_s_accounting_correction.json"
		)
		definition = json.loads(path.read_text())
		unique_key = next(
			field for field in definition["fields"] if field["fieldname"] == "unique_action_key"
		)
		self.assertEqual(unique_key["unique"], 1)
		for permission in definition["permissions"]:
			self.assertFalse(permission.get("write", 0))
			self.assertFalse(permission.get("create", 0))
			self.assertFalse(permission.get("delete", 0))

	def test_audit_controller_rejects_an_ordinary_save(self):
		audit = FourSAccountingCorrection(
			{
				"doctype": "Four S Accounting Correction",
				"reference_doctype": "Sales Invoice",
				"reference_name": "TEST-SI",
				"action": correction.CHANGE_POSTING_DATE,
				"company": correction.FOUR_S_COMPANY,
				"reason": "Attempted ordinary write",
				"performed_by": "Administrator",
				"performed_on": "2026-09-17 12:00:00",
				"unique_action_key": "a" * 64,
			}
		)
		audit.flags.ignore_links = True

		with (
			patch.dict(
				frappe.flags,
				{
					"in_four_s_accounting_correction": False,
					"in_install": False,
					"in_patch": False,
					"in_uninstall": False,
				},
			),
			patch.object(audit, "db_insert") as db_insert,
			self.assertRaises(frappe.PermissionError),
		):
			audit.save(ignore_permissions=True)

		db_insert.assert_not_called()

	def test_audit_controller_rejects_an_ordinary_delete(self):
		audit = FourSAccountingCorrection({"doctype": "Four S Accounting Correction"})

		with (
			patch.dict(
				frappe.flags,
				{
					"in_four_s_accounting_correction": False,
					"in_install": False,
					"in_patch": False,
					"in_uninstall": False,
				},
			),
			self.assertRaises(frappe.PermissionError),
		):
			audit.run_method("on_trash")

	def test_audit_controller_rejects_an_ordinary_rename(self):
		audit = FourSAccountingCorrection({"doctype": "Four S Accounting Correction"})

		with (
			patch.dict(
				frappe.flags,
				{
					"in_four_s_accounting_correction": False,
					"in_install": False,
					"in_patch": False,
					"in_uninstall": False,
				},
			),
			self.assertRaises(frappe.PermissionError),
		):
			audit.run_method("before_rename", "AUDIT-OLD", "AUDIT-NEW", False)

	def test_submitted_edit_bypasses_are_removed_by_installer(self):
		from fours_customizations import install

		deleted = []
		fake_db = SimpleNamespace(
			delete=lambda doctype, filters: deleted.append((doctype, filters)),
			commit=lambda: None,
		)
		fake_frappe = SimpleNamespace(db=fake_db, clear_cache=lambda **kwargs: None)
		with patch.object(install, "frappe", fake_frappe):
			install.lock_submitted_accounting_fields()

		self.assertIn(
			(
				"Property Setter",
				{
					"doc_type": "Sales Invoice",
					"field_name": "paid_amount",
					"property": "allow_on_submit",
				},
			),
			deleted,
		)
		self.assertIn(
			(
				"Property Setter",
				{
					"doc_type": "Sales Invoice Payment",
					"field_name": "amount",
					"property": "allow_on_submit",
				},
			),
			deleted,
		)
		self.assertTrue(all(doctype == "Property Setter" for doctype, _filters in deleted))
		self.assertEqual(
			len(deleted),
			len(
				{
					(
						filters["doc_type"],
						filters["field_name"],
						filters["property"],
					)
					for _doctype, filters in deleted
				}
			),
		)


if __name__ == "__main__":
	import unittest

	unittest.main()

"""Isolated exact-credit tests; no site, DB writes, queues or real production.

The existing held-correction fixture runs its genuine native tombstone guard.
Credit tests then exercise the real receipt signature, object capability,
commercial snapshots, source/child guards and transaction ownership.  Native
ERPNext production/accounting integration belongs to the root rollback probe.
"""

import json
import sys
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fours_customizations.tests import test_vox_pilot_correction as held


class CreditDocument(held.Document):
	def check_permission(self, permission):
		self.fixture.calls.append(("invoice_permission", permission))

	def save(self):
		credit = self.fixture.credit
		if self.doctype == "Kit POS Sale" and credit:
			credit.guard_credit_source_write(self, "validate")
		if self.doctype == "Sales Invoice":
			if len({row.warehouse for row in self.get("items")}) > 1:
				self.set_warehouse = None
			if self.is_pos == 0 and not self.get("payment_schedule"):
				self.set("payment_schedule", [])
				self.append(
					"payment_schedule",
					{
						"due_date": self.due_date,
						"invoice_portion": 100,
						"payment_amount": self.grand_total,
						"outstanding": self.grand_total,
						"paid_amount": 0,
					},
				)
			if self.fixture.change_due_date and self.is_pos == 0:
				self.due_date = "2026-10-01"
		return super().save()

	def insert(self, *, set_name):
		self.name, self.owner = set_name, self.fixture.frappe.session.user
		self.fixture.credit.guard_credit_approval(self, "validate")
		self.fixture.calls.append(("insert", self.doctype))
		self.fixture.records[(self.doctype, str(self.name))] = self.as_dict()
		return self

	def submit(self):
		fx = self.fixture
		fx.assertEqual(self.doctype, "Sales Invoice")
		fx.assertEqual((self.is_pos, self.is_created_using_pos), (0, 0))
		fx.assertFalse(self.payments)
		fx.assertIs(fx.submission_invoice, self)
		self.docstatus = 1
		source = fx.get_doc("Kit POS Sale", fx.module.SALE)
		fx.assertTrue(fx.credit.is_exact_approved_credit(self, source, fx.location))
		if fx.before_submit:
			fx.before_submit(self, source)
		if fx.submission_error:
			raise fx.submission_error
		fx.production_count += 1
		self.get("items")[0][fx.module.PRODUCTION_KEY] = "EXACT-NEW-PRODUCTION-KEY"
		self.get("items")[0][fx.module.WORK_ORDER_FIELD] = "MFG-WO-2026-00002"
		self.status = "Unpaid"
		fx.calls.append(("submit", "Sales Invoice"))
		fx.records[(self.doctype, str(self.name))] = self.as_dict()


class TestVoxPilotCredit(TestCase):
	def setUp(self):
		fx = self.fx = held.TestVoxPilotCorrection()
		fx.setUp()
		self.addCleanup(fx.doCleanups)
		fx.credit, fx.production_count, fx.change_due_date = None, 0, False
		fx.before_submit, fx.submission_error, fx.submission_invoice = None, None, None
		fx.finalized = False
		fx.get_doc = self.get_doc
		fx.frappe.get_doc = self.get_doc
		fx.frappe.conf = {"encryption_key": "only-a-synthetic-test-secret-32-characters"}
		fx.frappe.db.exists = lambda doctype, name: (doctype, str(name)) in fx.records
		fx.frappe.db.get_value = self.get_value
		fx.frappe.db.count = lambda doctype, filters: fx.effects.get(doctype, 0)
		sys.modules["frappe.utils"].now_datetime = lambda: datetime(2026, 9, 29, 13, 30, 0)
		fx.materialize._locked_sale = self.locked_sale
		fx.materialize._reject_finalized_opening = self.reject_finalized
		fx.materialize._location_for = lambda sale: fx.location
		fx.materialize._sales_team = lambda sale: [
			{"sales_person": "Original Waiter", "allocated_percentage": 100}
		]
		fx.materialize._submitting_invoice = self.submitting
		fx.materialize._invoice_submission_context = lambda invoice: (
			SimpleNamespace(invoice=invoice, sale=fx.submission_sale, user=fx.frappe.session.user)
			if fx.submission_invoice is invoice
			else None
		)
		fx.sales._authorize_sale_payload = self.authorize
		fx.work_orders._validate_pilot_retirement_work_order = lambda wo, state: self.assertEqual(
			(wo.docstatus, state), (2, "cancelled")
		)
		# Prepare the exact corrected held bill without touching any real site.
		fx.records[("Sales Invoice", fx.module.INVOICE)].update(
			due_date="2026-09-28",
			debit_to="Debtors - VXN",
			taxes=[],
			sales_team=[],
			payment_schedule=[],
			write_off_amount=0,
		)
		fx.module.apply_correction(expected_digest=fx.module.preview()["before_digest"])
		fx.records[("Material Request", "MAT-MR-2026-00001")] = {
			"doctype": "Material Request",
			"name": "MAT-MR-2026-00001",
			"docstatus": 2,
			"company": fx.module.COMPANY,
			"work_order": fx.module.PLANNING_WORK_ORDER,
		}
		self.enterContext(patch.dict(sys.modules, {"fours_customizations.vox_pilot_correction": fx.module}))
		import fours_customizations

		self.enterContext(patch.object(fours_customizations, "vox_pilot_correction", fx.module, create=True))
		path = Path(__file__).resolve().parents[1] / "vox_pilot_credit.py"
		self.credit = fx.credit = held.load_module("fours_customizations._isolated_vox_pilot_credit", path)
		self.addCleanup(sys.modules.pop, "fours_customizations._isolated_vox_pilot_credit", None)
		self.enterContext(patch.object(self.credit, "_production_evidence", side_effect=self.evidence))
		fx.calls.clear()

	def get_doc(self, doctype, name=None, **kwargs):
		values = doctype if isinstance(doctype, dict) else self.fx.records[(doctype, str(name))]
		doc = CreditDocument(self.fx, values)
		if doc.doctype == "Kit POS Sale":
			# Mirror native numeric hydration, distinct from a Python sum(0).
			doc.amount_paid = float(doc.amount_paid)
		return doc

	def get_value(self, doctype, name, field, **kwargs):
		record = self.fx.records.get((doctype, str(name)))
		if not record and doctype in {"Kit POS Sale Line", "Kit POS Sale Payment"}:
			table = "lines" if doctype == "Kit POS Sale Line" else "payments"
			record = next(
				(row for row in self.source().get(table) if str(row.get("name")) == str(name)), None
			)
			if record:
				record = {"parenttype": "Kit POS Sale", **record}
		if not record:
			return None
		if isinstance(field, list):
			return held.Record({key: record.get(key) for key in field})
		return record.get(field)

	def source(self):
		return self.fx.records[("Kit POS Sale", self.fx.module.SALE)]

	def invoice(self):
		return self.fx.records[("Sales Invoice", self.fx.module.INVOICE)]

	def locked_sale(self, name):
		self.fx.calls.append(("lock", "opening-sale"))
		return self.get_doc("Kit POS Sale", name)

	def reject_finalized(self, sale):
		if self.fx.finalized:
			raise ValueError("REGISTER_FINALIZED")

	def authorize(self, access, payload, *, sale):
		self.fx.calls.append(("authorize_payload",))
		if payload.get("is_suspend") == 0:
			access.require("sell.payments", "finalising a credit sale")
			if "disable_credit_sale" in self.fx.denied:
				raise PermissionError("disable_credit_sale")
		self.assertFalse(payload.get("payments"))

	@contextmanager
	def submitting(self, invoice, sale, location):
		self.fx.submission_invoice = invoice
		self.fx.submission_sale = sale
		try:
			yield
		finally:
			self.fx.submission_invoice = None
			self.fx.submission_sale = None

	def evidence(self, sale, invoice):
		self.assertEqual(self.fx.production_count, 1)
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(invoice.get("items")[0].get(self.fx.module.WORK_ORDER_FIELD), "MFG-WO-2026-00002")
		return {
			"work_order": "MFG-WO-2026-00002",
			"manufacture": "MAT-STE-2026-00001",
			"original_receivable": 54000,
		}

	def post(self):
		before = self.credit.preview_credit()["before_digest"]
		return before, self.credit.apply_credit(expected_digest=before)

	def verify(self):
		return self.credit.verify_posted_credit(
			self.get_doc("Kit POS Sale", self.fx.module.SALE),
			self.get_doc("Sales Invoice", self.fx.module.INVOICE),
		)

	def writes(self):
		return [
			value for value in self.fx.calls if value[0] in {"insert", "save", "submit", "commit", "enqueue"}
		]

	def test_preview_read_only_stable_and_no_receipt_secret_exposed(self):
		before = deepcopy(self.fx.records)
		first, second = self.credit.preview_credit(), self.credit.preview_credit()
		self.assertEqual(first, second)
		self.assertEqual(first["state"], "ready_credit")
		self.assertTrue(first["held"])
		self.assertFalse(first["submitted"])
		self.assertEqual(before, self.fx.records)
		self.assertEqual(self.writes(), [])
		self.assertNotIn(self.fx.frappe.conf["encryption_key"], repr(first))

	def test_exact_credit_uses_native_submit_due_date_and_real_zero_payments(self):
		lines = deepcopy(self.source()["lines"])
		_, result = self.post()
		self.assertEqual(result["state"], "posted_credit")
		self.assertEqual(result["credit_total"], 54000)
		self.assertEqual(result["outstanding_amount"], 54000)
		self.assertEqual(self.fx.production_count, 1)
		self.assertEqual(self.invoice()["docstatus"], 1)
		self.assertEqual(self.invoice()["due_date"], "2026-09-28")
		self.assertIsNone(self.invoice()["set_warehouse"])
		self.assertEqual((self.invoice()["is_pos"], self.invoice()["is_created_using_pos"]), (0, 0))
		self.assertEqual(
			(
				self.source()["status"],
				self.source()["payment_status"],
				self.source()["is_suspend"],
				self.source()["materialize_state"],
			),
			("final", "due", 0, "done"),
		)
		self.assertEqual(self.source()["amount_paid"], 0)
		self.assertEqual(self.source()["payment_revision"], 0)
		self.assertEqual(self.source()["payments"], [])
		self.assertEqual(self.invoice()["payments"], [])
		self.assertEqual(lines, self.source()["lines"])
		self.assertNotIn("line_removal_approvals", json.loads(self.source()["payload"]))
		self.assertTrue(self.verify())
		self.assertFalse(any(value[0] in {"commit", "enqueue"} for value in self.fx.calls))

	def test_repeat_original_digest_is_verified_read_only_no_recooking(self):
		digest, _ = self.post()
		before, writes = deepcopy(self.fx.records), list(self.writes())
		result = self.credit.apply_credit(expected_digest=digest)
		self.assertEqual(result["state"], "already_posted")
		self.assertEqual(self.fx.production_count, 1)
		self.assertEqual(self.fx.records, before)
		self.assertEqual(self.writes(), writes)

	def test_changed_preview_digest_fails_before_any_write(self):
		self.credit.preview_credit()
		with self.assertRaises(self.credit.CreditError):
			self.credit.apply_credit(expected_digest="0" * 64)
		self.assertEqual(self.writes(), [])

	def test_native_credit_permission_and_control_still_required(self):
		for denied in ("sell.update", "sell.payments", "disable_credit_sale"):
			with self.subTest(denied=denied):
				self.fx.denied = {denied}
				with self.assertRaises(PermissionError):
					self.credit.preview_credit()
		self.assertEqual(self.writes(), [])

	def test_original_held_draft_has_no_credit_grant(self):
		self.assertFalse(self.verify())
		self.assertEqual(self.writes(), [])

	def test_client_flags_cannot_create_credit_authority(self):
		sale = self.get_doc("Kit POS Sale", self.fx.module.SALE)
		invoice = self.get_doc("Sales Invoice", self.fx.module.INVOICE)
		sale.is_suspend, sale.materialize_state = 0, "pending"
		payload = json.loads(sale.payload)
		payload["is_suspend"] = 0
		sale.payload = json.dumps(payload)
		invoice.is_pos = invoice.is_created_using_pos = 0
		invoice.flags.credit_approved = True
		invoice.flags.vox_pilot_credit = {"approved_by": "Administrator", "approved": True}
		with self.assertRaises(self.credit.CreditError):
			self.credit.is_exact_approved_credit(invoice, sale, self.fx.location)

	def test_signed_receipt_does_not_authorize_a_copied_initial_invoice(self):
		def examine(invoice, source):
			clone = CreditDocument(self.fx, invoice.as_dict())
			with self.assertRaises(self.credit.CreditError):
				self.credit.is_exact_approved_credit(clone, source, self.fx.location)

		self.fx.before_submit = examine
		self.post()

	def test_initial_grant_also_requires_native_submitting_context_and_docstatus(self):
		def examine(invoice, source):
			self.fx.submission_invoice = None
			with self.assertRaises(self.credit.CreditError):
				self.credit.is_exact_approved_credit(invoice, source, self.fx.location)
			self.fx.submission_invoice = invoice
			invoice.docstatus = 0
			with self.assertRaises(self.credit.CreditError):
				self.credit.is_exact_approved_credit(invoice, source, self.fx.location)
			invoice.docstatus = 1

		self.fx.before_submit = examine
		self.post()

	def test_native_failure_propagates_caller_rolls_back_and_context_is_released(self):
		before, commit = deepcopy(self.fx.records), self.fx.frappe.db.commit
		self.fx.submission_error = ValueError("native stock shortage")
		with self.assertRaisesRegex(ValueError, "native stock shortage"):
			self.post()
		self.assertIsNone(self.credit._context())
		self.assertIs(self.fx.frappe.db.commit, commit)
		self.assertEqual(self.fx.production_count, 0)
		self.assertFalse(any(value[0] in {"commit", "enqueue"} for value in self.fx.calls))
		# The helper deliberately does not own rollback; model caller rollback.
		self.fx.records = deepcopy(before)
		self.assertEqual(self.fx.records, before)

	def test_attempted_native_commit_is_stopped_and_not_silently_accepted(self):
		self.fx.before_submit = lambda invoice, sale: self.fx.frappe.db.commit()
		with self.assertRaises(self.fx.module.CorrectionError):
			self.post()
		self.assertIsNone(self.credit._context())
		self.assertEqual(self.fx.production_count, 0)

	def test_native_due_date_change_requires_review_not_implicit_acceptance(self):
		self.fx.change_due_date = True
		with self.assertRaisesRegex(self.credit.CreditError, "due date"):
			self.post()
		self.assertEqual(self.fx.production_count, 0)

	def test_posted_source_commercial_and_tender_mutation_fails(self):
		self.post()
		changes = (
			{"is_suspend": 1},
			{"final_total": 50000},
			{"amount_paid": 54000},
			{"payment_status": "paid"},
			{"deleted_at": "2026-09-29 14:00:00"},
			{"merged_into": 5000},
			{"customer": "Another Customer"},
			{"materialize_state": "draft"},
		)
		for change in changes:
			with self.subTest(change=change):
				doc = self.get_doc("Kit POS Sale", self.fx.module.SALE)
				doc.update(change)
				with self.assertRaises(self.credit.CreditError):
					self.credit.guard_credit_source_write(doc, "validate")
		doc = self.get_doc("Kit POS Sale", self.fx.module.SALE)
		doc.lines[0].unit_price = 1
		with self.assertRaises(self.credit.CreditError):
			self.credit.guard_credit_source_write(doc, "validate")

	def test_source_delete_or_rename_rejected_with_extra_native_args(self):
		self.post()
		doc = self.get_doc("Kit POS Sale", self.fx.module.SALE)
		for method in ("on_trash", "before_rename"):
			with self.assertRaises(self.credit.CreditError):
				self.credit.guard_credit_source_write(doc, method, "4929", "5001", False)

	def test_direct_child_edit_insertion_deletion_or_detachment_is_protected(self):
		self.post()
		source = self.get_doc("Kit POS Sale", self.fx.module.SALE)
		for doctype in ("Kit POS Sale Line", "Kit POS Sale Payment"):
			doc = held.Record(
				doctype=doctype, name="direct-child", parent="4929", parenttype="Kit POS Sale", amount=54000
			)
			for method in ("validate", "on_trash"):
				with self.subTest(doctype=doctype, method=method), self.assertRaises(self.credit.CreditError):
					self.credit.guard_credit_source_write(doc, method)
		old = source.lines[1]
		detached = held.Record(old, doctype="Kit POS Sale Line", parent="9999", parenttype="Kit POS Sale")
		with self.assertRaises(self.credit.CreditError):
			self.credit.guard_credit_source_write(detached, "validate")

	def test_approval_cannot_be_forged_edited_deleted_or_renamed(self):
		forged = held.Record(doctype="Comment", name=self.credit.RECEIPT)
		with self.assertRaises(self.credit.CreditError):
			self.credit.guard_credit_approval(forged, "validate")
		self.post()
		doc = self.get_doc("Comment", self.credit.RECEIPT)
		for method in ("validate", "on_trash", "before_rename"):
			with self.assertRaises(self.credit.CreditError):
				self.credit.guard_credit_approval(doc, method, self.credit.RECEIPT, "other", False)

	def test_reserved_approval_destination_cannot_be_occupied_before_creation(self):
		doc = held.Record(doctype="Comment", name="ordinary-comment")
		for destination in (self.credit.RECEIPT, f"  {self.credit.RECEIPT}  ", self.credit.RECEIPT.lower()):
			for merge in (False, True):
				with (
					self.subTest(destination=destination, merge=merge),
					self.assertRaises(self.credit.CreditError),
				):
					self.credit.guard_credit_approval(doc, "before_rename", doc.name, destination, merge)
		with self.assertRaises(self.credit.CreditError):
			self.credit.guard_credit_approval(
				doc, "before_rename", old=doc.name, new=self.credit.RECEIPT, merge=False
			)
		self.assertFalse(self.fx.frappe.db.exists("Comment", self.credit.RECEIPT))
		self.assertEqual(self.writes(), [])

	def test_unrelated_comment_cannot_merge_into_existing_approval(self):
		self.post()
		before = deepcopy(self.fx.records)
		doc = held.Record(doctype="Comment", name="ordinary-comment")
		with self.assertRaises(self.credit.CreditError):
			self.credit.guard_credit_approval(doc, "before_rename", doc.name, self.credit.RECEIPT, True)
		self.assertEqual(self.fx.records, before)

	def test_destination_reservation_is_limited_to_exact_site_comment_identity(self):
		doc = held.Record(doctype="Comment", name="ordinary-comment")
		self.credit.guard_credit_approval(doc, "before_rename", doc.name, "another-comment", False)
		self.credit.guard_credit_approval(
			doc, "before_rename", doc.name, self.credit.RECEIPT + "-other", False
		)
		self.credit.guard_credit_approval(
			held.Record(doctype="Note", name="ordinary-note"),
			"before_rename",
			"ordinary-note",
			self.credit.RECEIPT,
			False,
		)
		self.fx.frappe.local.site = "another.kit.africa"
		self.credit.guard_credit_approval(doc, "before_rename", doc.name, self.credit.RECEIPT, False)
		self.assertEqual(self.writes(), [])

	def test_missing_or_tampered_receipt_fails_closed_not_normal_fallthrough(self):
		self.post()
		key = ("Comment", self.credit.RECEIPT)
		original = deepcopy(self.fx.records[key])
		del self.fx.records[key]
		with self.assertRaises(self.credit.CreditError):
			self.verify()
		self.fx.records[key] = deepcopy(original)
		envelope = json.loads(original["content"])
		envelope["approval"]["credit_total"] = "1"
		self.fx.records[key]["content"] = json.dumps(envelope)
		with self.assertRaises(self.credit.CreditError):
			self.verify()

	def test_approval_creator_and_reference_are_not_client_assertions(self):
		self.post()
		record = self.fx.records[("Comment", self.credit.RECEIPT)]
		original = deepcopy(record)
		for field, value in (
			("owner", "cashier@example.com"),
			("comment_email", "cashier@example.com"),
			("reference_name", "SI-OTHER"),
			("published", 1),
		):
			record.clear()
			record.update(original)
			record[field] = value
			with self.subTest(field=field), self.assertRaises(self.credit.CreditError):
				self.verify()

	def test_later_native_payment_and_closing_links_do_not_revoke_credit_history(self):
		self.post()
		self.invoice().update(
			outstanding_amount=10000, status="Partly Paid", pos_closing_entry="POS-CLO-2026-00001"
		)
		self.invoice()["payment_schedule"][0].update(outstanding=10000, paid_amount=44000)
		self.fx.finalized = True
		self.assertTrue(self.verify())
		self.invoice().update(outstanding_amount=0, status="Paid", pos_closing_entry=None)
		self.assertTrue(self.verify())
		self.assertEqual(self.source()["amount_paid"], 0)
		self.assertEqual(self.source()["payments"], [])

	def test_invoice_guard_is_inert_before_credit_approval(self):
		doc = self.get_doc("Sales Invoice", self.fx.module.INVOICE)
		before = deepcopy(self.fx.records)
		for method in ("before_cancel", "on_trash", "before_rename", "before_update_after_submit"):
			self.credit.guard_credit_invoice(doc, method, doc.name, "SI-OTHER", False)
		self.assertEqual(self.fx.records, before)
		self.assertEqual(self.writes(), [])

	def test_credit_invoice_cannot_be_cancelled_deleted_or_renamed(self):
		self.post()
		before = deepcopy(self.fx.records)
		for method in ("before_cancel", "on_trash", "before_rename"):
			doc = self.get_doc("Sales Invoice", self.fx.module.INVOICE)
			if method == "before_cancel":
				doc.docstatus = 2  # Native _cancel sets this before its hook.
			doc.flags.ignore_permissions = True
			doc.flags.credit_approved = True
			with self.subTest(method=method), self.assertRaises(self.credit.CreditError):
				self.credit.guard_credit_invoice(doc, method, doc.name, "SI-RENAMED", False)
		self.assertEqual(self.fx.records, before)

	def test_posted_invoice_commercial_changes_are_rejected_before_save(self):
		self.post()
		changes = (
			{"due_date": "2026-12-01"},
			{"customer": "Another Customer"},
			{"grand_total": 1},
			{"is_pos": 1},
			{"is_created_using_pos": 1},
			{"paid_amount": 54000},
		)
		before = deepcopy(self.fx.records)
		for change in changes:
			doc = self.get_doc("Sales Invoice", self.fx.module.INVOICE)
			doc.update(change)
			with self.subTest(change=change), self.assertRaises(self.credit.CreditError):
				self.credit.guard_credit_invoice(doc, "before_update_after_submit")
		doc = self.get_doc("Sales Invoice", self.fx.module.INVOICE)
		doc.get("items")[1].rate = 1
		with self.assertRaises(self.credit.CreditError):
			self.credit.guard_credit_invoice(doc, "before_update_after_submit")
		self.assertEqual(self.fx.records, before)

	def test_invoice_guard_allows_native_collections_and_closing_links(self):
		self.post()
		before = deepcopy(self.fx.records)
		doc = self.get_doc("Sales Invoice", self.fx.module.INVOICE)
		doc.update(outstanding_amount=10000, status="Partly Paid", pos_closing_entry="POS-CLO-2026-00001")
		doc.get("payment_schedule")[0].update(outstanding=10000, paid_amount=44000)
		self.credit.guard_credit_invoice(doc, "before_update_after_submit")
		doc.update(outstanding_amount=0, status="Paid", pos_closing_entry=None)
		doc.get("payment_schedule")[0].update(outstanding=0, paid_amount=54000)
		self.credit.guard_credit_invoice(doc, "before_update_after_submit")
		self.assertEqual(self.fx.records, before)

	def test_invoice_guard_initial_update_requires_exact_live_submitting_object(self):
		def examine(invoice, source):
			self.credit.guard_credit_invoice(invoice, "before_update_after_submit")
			clone = CreditDocument(self.fx, invoice.as_dict())
			clone.flags.credit_approved = True
			with self.assertRaises(self.credit.CreditError):
				self.credit.guard_credit_invoice(clone, "before_update_after_submit")
			with self.assertRaises(self.credit.CreditError):
				self.credit.guard_credit_invoice(invoice, "before_cancel")

		self.fx.before_submit = examine
		self.post()

	def test_invoice_guard_does_not_fall_through_when_posted_proof_is_missing(self):
		self.post()
		del self.fx.records[("Comment", self.credit.RECEIPT)]
		doc = self.get_doc("Sales Invoice", self.fx.module.INVOICE)
		with self.assertRaises(self.credit.CreditError):
			self.credit.guard_credit_invoice(doc, "before_update_after_submit")
		with self.assertRaises(self.credit.CreditError):
			self.credit.guard_credit_invoice(doc, "before_cancel")

	def test_invoice_guard_protects_only_exact_posted_identity_and_rename_destination(self):
		self.post()
		doc = held.Record(doctype="Sales Invoice", name="SI-OTHER")
		self.credit.guard_credit_invoice(doc, "before_cancel")
		self.credit.guard_credit_invoice(doc, "before_rename", doc.name, "SI-ANOTHER", False)
		for destination in (self.fx.module.INVOICE, f"  {self.fx.module.INVOICE.lower()}  "):
			with self.subTest(destination=destination), self.assertRaises(self.credit.CreditError):
				self.credit.guard_credit_invoice(doc, "before_rename", doc.name, destination, True)
		self.credit.guard_credit_invoice(
			held.Record(doctype="Purchase Invoice", name=self.fx.module.INVOICE), "before_cancel"
		)
		self.fx.frappe.local.site = "another.kit.africa"
		self.credit.guard_credit_invoice(
			held.Record(doctype="Sales Invoice", name=self.fx.module.INVOICE), "before_cancel"
		)

	def test_initial_finalized_opening_is_rejected(self):
		before = self.credit.preview_credit()["before_digest"]
		self.fx.finalized = True
		with self.assertRaisesRegex(ValueError, "REGISTER_FINALIZED"):
			self.credit.apply_credit(expected_digest=before)
		self.assertEqual(self.writes(), [])

	def test_unrelated_sale_comments_children_not_affected(self):
		self.assertFalse(
			self.credit.verify_posted_credit(held.Record(name=2128), held.Record(name="SI-OTHER"))
		)
		self.credit.guard_credit_approval(held.Record(doctype="Comment", name="ordinary-comment"), "on_trash")
		self.credit.guard_credit_source_write(held.Record(doctype="Kit POS Sale", name=2128), "on_trash")
		self.credit.guard_credit_source_write(
			held.Record(
				doctype="Kit POS Sale Line", name="unrelated", parent=2128, parenttype="Kit POS Sale"
			),
			"validate",
		)

	def test_cancelled_procurement_is_a_precondition_not_cancelled_by_credit(self):
		self.fx.records[("Material Request", self.credit.MATERIAL_REQUEST)]["docstatus"] = 1
		with self.assertRaises(self.credit.CreditError):
			self.credit.preview_credit()
		self.assertEqual(self.writes(), [])

	def test_document_flags_or_new_payment_cannot_change_posted_invoice(self):
		self.post()
		original = deepcopy(self.invoice())
		for change in (
			{"is_pos": 1},
			{"is_created_using_pos": 1},
			{"customer": "Another Customer"},
			{"due_date": "2026-12-01"},
			{"paid_amount": 1},
			{"grand_total": 54001},
		):
			self.invoice().clear()
			self.invoice().update(original)
			self.invoice().update(change)
			with self.subTest(change=change), self.assertRaises(self.credit.CreditError):
				self.verify()

	def test_integer_float_hydration_has_stable_but_typed_fingerprint(self):
		self.assertEqual(self.credit._digest({"amount": 0}), self.credit._digest({"amount": 0.0}))
		self.assertNotEqual(self.credit._digest({"amount": 0}), self.credit._digest({"amount": "0"}))
		self.post()
		self.source()["amount_paid"] = 0.0
		self.invoice()["grand_total"] = 54000.0
		self.assertTrue(self.verify())

	def test_only_administrator_exact_site_can_start_completion(self):
		self.fx.frappe.session.user = "cashier@example.com"
		with self.assertRaises(self.fx.module.CorrectionError):
			self.credit.preview_credit()
		self.fx.frappe.session.user = "Administrator"
		self.fx.frappe.local.site = "another.kit.africa"
		with self.assertRaises(self.fx.module.CorrectionError):
			self.credit.preview_credit()

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from fours_customizations import cancellation_override
from fours_customizations import sales_chain_integrity as integrity


class TestNansanaPaidCancellation(IntegrationTestCase):
	def _invoice(self, *, company=integrity.VOX_NANSANA_COMPANY):
		return frappe._dict(
			name="TEST-NANSANA-SI",
			company=company,
			docstatus=1,
			is_pos=1,
			is_return=0,
			paid_amount=25_000,
			payments=[frappe._dict(amount=25_000)],
			flags=frappe._dict(),
		)

	@staticmethod
	def _no_linked_documents(doctype, **kwargs):
		return []

	def test_configured_auto_cancel_role_allows_only_embedded_payment(self):
		doc = self._invoice()
		with (
			patch.object(integrity.frappe, "get_all", side_effect=self._no_linked_documents),
			patch.object(integrity.frappe.db, "get_value", return_value=25_000),
			patch.object(integrity.frappe, "has_permission", return_value=True),
			patch("cancellation_requests.utils.can_user_auto_cancel", return_value=True),
			patch("cancellation_requests.utils.can_user_cancel_doctype", return_value=False),
		):
			self.assertTrue(
				integrity.can_auto_cancel_nansana_embedded_payment(doc, "vox@example.com")
			)

	def test_other_company_is_not_allowed(self):
		doc = self._invoice(company="4S Industries Limited")
		self.assertFalse(
			integrity.can_auto_cancel_nansana_embedded_payment(doc, "vox@example.com")
		)

	def test_non_submitted_invoice_is_not_allowed(self):
		doc = self._invoice()
		doc.docstatus = 2
		self.assertFalse(
			integrity.can_auto_cancel_nansana_embedded_payment(doc, "vox@example.com")
		)

	def test_external_payment_entry_is_not_allowed(self):
		doc = self._invoice()

		def get_all(doctype, **kwargs):
			if doctype == "Payment Entry Reference":
				return ["PAYMENT-REFERENCE"]
			return []

		with (
			patch.object(integrity.frappe, "get_all", side_effect=get_all),
			patch.object(integrity.frappe, "has_permission", return_value=True),
			patch("cancellation_requests.utils.can_user_auto_cancel", return_value=True),
			patch("cancellation_requests.utils.can_user_cancel_doctype", return_value=False),
		):
			self.assertFalse(
				integrity.can_auto_cancel_nansana_embedded_payment(doc, "vox@example.com")
			)

	def test_user_without_configured_auto_cancel_role_is_not_allowed(self):
		doc = self._invoice()
		with (
			patch("cancellation_requests.utils.can_user_auto_cancel", return_value=False),
			patch("cancellation_requests.utils.can_user_cancel_doctype", return_value=False),
		):
			self.assertFalse(
			integrity.can_auto_cancel_nansana_embedded_payment(doc, "ordinary@example.com")
		)

	def test_user_without_company_access_is_not_allowed(self):
		doc = self._invoice()
		with (
			patch.object(integrity.frappe, "has_permission", return_value=False),
			patch("cancellation_requests.utils.can_user_auto_cancel", return_value=True),
			patch("cancellation_requests.utils.can_user_cancel_doctype", return_value=False),
		):
			self.assertFalse(
				integrity.can_auto_cancel_nansana_embedded_payment(doc, "4s@example.com")
			)

	def test_request_marker_is_invoice_scoped_and_restored(self):
		doc = self._invoice()
		marker = integrity.VOX_NANSANA_PAID_CANCEL_FLAG
		frappe.flags[marker] = "OUTER-INVOICE"

		def original(doctype, name, reason):
			self.assertEqual(doctype, "Sales Invoice")
			self.assertEqual(name, doc.name)
			self.assertEqual(frappe.flags.get(marker), doc.name)
			return {"auto_cancelled": True}

		with (
			patch.object(cancellation_override.frappe, "get_doc", return_value=doc),
			patch.object(
				cancellation_override,
				"can_auto_cancel_nansana_embedded_payment",
				return_value=True,
			),
			patch("cancellation_requests.api.request_cancellation", side_effect=original),
		):
			result = cancellation_override._try_auto_cancel_nansana_paid_invoice(
				doc.name, "A sufficiently detailed cancellation reason for this bill."
			)

		self.assertEqual(result, {"auto_cancelled": True})
		self.assertEqual(frappe.flags.get(marker), "OUTER-INVOICE")

	def test_request_marker_is_restored_when_cancellation_raises(self):
		doc = self._invoice()
		marker = integrity.VOX_NANSANA_PAID_CANCEL_FLAG
		frappe.flags.pop(marker, None)

		def original(doctype, name, reason):
			self.assertEqual(frappe.flags.get(marker), doc.name)
			raise RuntimeError("cancel failed")

		with (
			patch.object(cancellation_override.frappe, "get_doc", return_value=doc),
			patch.object(
				cancellation_override,
				"can_auto_cancel_nansana_embedded_payment",
				return_value=True,
			),
			patch("cancellation_requests.api.request_cancellation", side_effect=original),
			self.assertRaisesRegex(RuntimeError, "cancel failed"),
		):
			cancellation_override._try_auto_cancel_nansana_paid_invoice(
				doc.name, "A sufficiently detailed cancellation reason for this bill."
			)

		self.assertNotIn(marker, frappe.flags)

	def test_scoped_marker_survives_the_before_cancel_docstatus_transition(self):
		doc = self._invoice()
		doc.docstatus = 2  # Frappe sets this before running before_cancel.
		marker = integrity.VOX_NANSANA_PAID_CANCEL_FLAG
		frappe.flags[marker] = doc.name
		try:
			with (
				patch.object(integrity.frappe, "get_all", side_effect=self._no_linked_documents),
				patch.object(integrity.frappe.db, "get_value", return_value=25_000),
				patch.object(integrity.frappe, "has_permission", return_value=True),
				patch("cancellation_requests.utils.can_user_auto_cancel", return_value=True),
				patch("cancellation_requests.utils.can_user_cancel_doctype", return_value=False),
			):
				integrity.validate_no_payment_allocated(doc)

			self.assertTrue(doc.flags.allow_cancel_with_payment)
		finally:
			frappe.flags.pop(marker, None)

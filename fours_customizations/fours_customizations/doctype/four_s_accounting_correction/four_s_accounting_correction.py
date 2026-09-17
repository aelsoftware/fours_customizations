import frappe
from frappe import _
from frappe.model.document import Document


class FourSAccountingCorrection(Document):
	"""Immutable audit evidence written only by the controlled correction API."""

	def validate(self):
		self._validate_internal_write()

	def on_trash(self):
		self._validate_internal_write()

	def before_rename(self, old_name, new_name, merge=False):
		self._validate_internal_write()

	@staticmethod
	def _validate_internal_write():
		if (
			frappe.flags.get("in_four_s_accounting_correction")
			or frappe.flags.in_install
			or frappe.flags.in_patch
			or frappe.flags.get("in_uninstall")
		):
			return

		frappe.throw(
			_("Four S Accounting Correction records are immutable audit records."),
			frappe.PermissionError,
		)

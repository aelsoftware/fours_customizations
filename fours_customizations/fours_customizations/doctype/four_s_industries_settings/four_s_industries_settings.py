"""
4S Industries Settings — Single doctype that centralises automation config.

A thin helper layer (`get_settings`, `get_setting`) is provided so handlers can
read configuration without having to know the exact field names.
"""

import frappe
from frappe.model.document import Document


class FourSIndustriesSettings(Document):
	def validate(self):
		self._validate_payroll_day()
		self._validate_times()

	def _validate_payroll_day(self):
		day = int(self.payroll_day_of_month or 0)
		if day < 1 or day > 31:
			frappe.throw("Payroll Day of Month must be between 1 and 31.")

	def _validate_times(self):
		# Times are stored as strings; Frappe will validate format. Nothing extra
		# to enforce — we tolerate sensible time ordering rather than fight it.
		return


def get_settings():
	"""Return the cached settings doc. Safe to call from any context."""
	return frappe.get_cached_doc("4S Industries Settings")


def get_setting(field, default=None):
	"""Get one field from the settings doc; falls back to `default`."""
	try:
		value = frappe.db.get_single_value("4S Industries Settings", field)
	except Exception:
		value = None
	if value in (None, ""):
		return default
	return value

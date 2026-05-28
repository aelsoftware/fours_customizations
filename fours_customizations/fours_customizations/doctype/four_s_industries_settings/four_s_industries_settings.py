"""
Fours Industries Settings — Single doctype that centralises automation config.

A thin helper layer (`get_settings`, `get_setting`) is provided so handlers can
read configuration without having to know the exact field names.
"""

import frappe
from frappe.model.document import Document


PAYROLL_DAY_OPTIONS = {
	"1st day of next month",
	"2nd day of next month",
	"3rd day of next month",
	"4th day of next month",
	"Last day of payroll period month",
}


class FourSIndustriesSettings(Document):
	def validate(self):
		self._validate_payroll_day()

	def _validate_payroll_day(self):
		day = (self.payroll_day_of_month or "").strip()
		if day and day not in PAYROLL_DAY_OPTIONS:
			frappe.throw(f"Payroll Run Day {day!r} is not one of the supported options.")


def get_settings():
	"""Return the cached settings doc. Safe to call from any context."""
	return frappe.get_cached_doc("Fours Industries Settings")


def get_setting(field, default=None):
	"""Get one field from the settings doc; falls back to `default`."""
	try:
		value = frappe.db.get_single_value("Fours Industries Settings", field)
	except Exception:
		value = None
	if value in (None, ""):
		return default
	return value

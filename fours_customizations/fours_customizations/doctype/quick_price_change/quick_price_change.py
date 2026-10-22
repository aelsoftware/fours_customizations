import frappe
from frappe import _
from frappe.model.document import Document


class QuickPriceChange(Document):
	"""Audit row of one price saved on the Quick Price Update page (fours_customizations.quick_price.api).

	Read: everyone with Item Price read (has_permission / query_conditions below; the DocPerm gives All read).
	Create: only the page's API (flag set there; no role has create). Edit / delete: System Manager only."""

	def validate(self):
		if self.is_new():
			if not (self.flags.from_quick_price_update or frappe.flags.in_install or frappe.flags.in_patch):
				frappe.throw(
					_("Quick Price Change rows are written by Quick Price Update only."),
					frappe.PermissionError,
				)
		elif not is_system_manager():
			frappe.throw(_("Quick Price Change rows cannot be edited."), frappe.PermissionError)

	def on_trash(self):
		if not is_system_manager():
			frappe.throw(_("Quick Price Change rows cannot be deleted."), frappe.PermissionError)


def is_system_manager(user=None):
	user = user or frappe.session.user
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def can_read_log(user):
	from fours_customizations.quick_price.api import can_read_prices

	return is_system_manager(user) or can_read_prices(user)


def has_permission(doc, ptype=None, user=None, debug=False):
	"""The log is for people who may read Item Prices (DocPerm read is 'All': this narrows it)."""
	user = user or frappe.session.user
	if ptype in ("write", "delete", "cancel", "submit", "amend", "create"):
		return is_system_manager(user)
	return can_read_log(user)


def query_conditions(user=None, doctype=None):
	user = user or frappe.session.user
	return "" if can_read_log(user) else "1=0"

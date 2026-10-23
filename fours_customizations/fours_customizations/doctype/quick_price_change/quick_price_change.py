import frappe
from frappe import _
from frappe.model.document import Document


class QuickPriceChange(Document):
	"""Audit row of one price saved on the Quick Price Update page (fours_customizations.quick_price.api).

	Read: everyone with Item Price read (has_permission / query_conditions below; the DocPerm gives All read); with the
	price list split active, a company-restricted user reads only rows of their companies on lists of no company or of
	theirs (price_lists.allowed_companies: also Company rows scoped to Item Price / Price List).
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


def company_scope(user):
	"""None (every row) or (the user's companies, the lists of other companies) while the price list split is active."""
	from fours_customizations import price_lists

	if not price_lists.split_active():
		return None
	allowed = price_lists.allowed_companies(user)
	if allowed is None:
		return None
	foreign = {n for n, info in price_lists.lists().items() if info.company and info.company not in allowed}
	return allowed, foreign


def has_permission(doc, ptype=None, user=None, debug=False):
	"""The log is for people who may read Item Prices (DocPerm read is 'All': this narrows it)."""
	user = user or frappe.session.user
	if ptype in ("write", "delete", "cancel", "submit", "amend", "create"):
		return is_system_manager(user)
	if not can_read_log(user):
		return False
	scope = company_scope(user)
	if scope is None:
		return True
	allowed, foreign = scope
	return doc.get("company") in allowed and doc.get("price_list") not in foreign


def query_conditions(user=None, doctype=None):
	user = user or frappe.session.user
	if not can_read_log(user):
		return "1=0"
	scope = company_scope(user)
	if scope is None:
		return ""
	allowed, foreign = scope
	condition = f"`tabQuick Price Change`.`company` in ({', '.join(frappe.db.escape(c) for c in sorted(allowed))})"
	if foreign:
		condition += (
			" and ifnull(`tabQuick Price Change`.`price_list`, '') not in"
			f" ({', '.join(frappe.db.escape(n) for n in sorted(foreign))})"
		)
	return f"({condition})"

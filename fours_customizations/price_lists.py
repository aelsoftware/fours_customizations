"""Company-owned price lists (price list split, ps-fours-2).

A Price List belongs to a company when its custom_company (Custom Field, created by the split's data step) is set. The
split is ACTIVE once that field exists and at least one list has a company; until then every function here is a no-op
and the site behaves as before the release.

With the split active:
- Item Price read/write/create/delete for a company-restricted user (a Company User Permission that applies to all
  doctypes, Item Price or Price List) covers only rows of ENABLED lists whose company is one of theirs, or rows of
  blank-company lists (Standard Buying, ...) whose item belongs to one of their companies' item families. Administrator,
  users without such a row and Kit POS device sessions keep full access.
- Price List: only Administrator or an unrestricted System Manager may rename a list or change its title, enabled,
  selling, buying, currency, price_not_uom_dependent or custom_company; a new selling list needs a company; a list stays enabled
  while an active Kit POS Location or an enabled POS Profile uses it.
- Kit POS Location / POS Profile use an enabled list of their own company (or a blank-company list).
- Item Price in a company list: the item must be of that company's family (allowlist; site_config
  fours_price_list_item_guard = 0 turns it off).
- Customer / Customer Group: a party default list is blank or enabled; a restricted user sets only a list of their
  companies (Supplier / Supplier Group: also a list of no company, e.g. Standard Buying).
- New selling documents and fours' document copies leave a DISABLED selling list for the company's default list
  (Company.custom_default_selling_price_list) without re-pricing their lines; new Items' Item Defaults get that list.
- ERPNext's automatic price writes (the auto-insert of a missing price while a document is priced, a new Item's
  Standard Selling Rate) are SKIPPED, not failed, when their list is disabled, the item is outside the list's family,
  or the session user (ERPNext writes as that user) may not use that row: documents mapped from pre-split documents
  (priced on the server before any hook of the new document runs), sales of an item of another family and a restricted
  writer's purchase of an item outside their families still save. patch_erpnext() installs that guard (on import,
  before_request, before_job) only while ERPNext's functions have the signatures it wraps.

Lists, companies and item families are read once per request (frappe.local) and per user, so saleslive's per-user
read checks of one Item Price stay cheap.
"""

import frappe
from frappe import _
from frappe.utils import cint

FIELD = "custom_company"  # Price List -> Company (the data step's Custom Field)
DEFAULT_FIELD = "custom_default_selling_price_list"  # Company -> Price List (this release's Custom Field)
GUARD_CONFIG = "fours_price_list_item_guard"  # site_config: 0 turns the item-family guard off
FAMILIES_CONFIG = "fours_price_list_families"  # site_config: {company: [root item groups]} replaces FAMILIES
# Item families: an item belongs to a company's family when its Item Group is in the subtree of one of these roots.
FAMILIES = {
	"4S Industries Limited": ("4S Items",),
	"Vox Lounge": ("Vox Items", "Raw Kitchen Items"),
	"Vox Lounge Nansana": ("Vox Items", "Raw Kitchen Items"),
}
# Company User Permissions that restrict prices: rows for all doctypes, Item Price or Price List.
APPLICABLE_FOR = ("", "Item Price", "Price List")
GUARDED_FIELDS = ("price_list_name", "enabled", "selling", "buying", "currency", "price_not_uom_dependent", FIELD)
CHECK_FIELDS = ("enabled", "selling", "buying", "price_not_uom_dependent")
READ_PTYPES = ("read", "select", "print", "email", "export", "report", "share")
SELLING_DOCTYPES = ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "POS Invoice")


class ItemFamilyError(frappe.ValidationError):
	pass


# -- state ------------------------------------------------------------------------------------------------------------


def _memo():
	memo = getattr(frappe.local, "fours_price_lists", None)
	if memo is None:
		memo = frappe.local.fours_price_lists = {}
	return memo


def clear_caches():
	"""Forget this request's lists/companies/families and the Quick Price page's cached usage (data kit, tests)."""
	frappe.local.fours_price_lists = {}
	frappe.cache.delete_keys("fours_quick_price:")


def forget(doc=None, method=None, *args, **kwargs):
	"""Price List / Company changed in this request: re-read lists and defaults."""
	frappe.local.fours_price_lists = {}


def has_company_field():
	return frappe.get_meta("Price List").has_field(FIELD)


def lists():
	"""{name: {company, enabled, selling, buying, currency}} of every Price List (company None without the field)."""
	memo = _memo()
	if "lists" not in memo:
		fields = ["name", "enabled", "selling", "buying", "currency"]
		if has_company_field():
			fields.append(FIELD)
		memo["lists"] = {
			row.name: frappe._dict(
				company=row.get(FIELD) or None,
				enabled=cint(row.enabled),
				selling=cint(row.selling),
				buying=cint(row.buying),
				currency=row.currency,
			)
			for row in frappe.get_all("Price List", fields=fields)
		}
	return memo["lists"]


def list_info(price_list):
	return lists().get(price_list) if price_list else None


def split_active():
	"""The custom_company field exists and at least one list has a company."""
	memo = _memo()
	if "active" not in memo:
		memo["active"] = has_company_field() and any(info.company for info in lists().values())
	return memo["active"]


def families():
	configured = frappe.conf.get(FAMILIES_CONFIG)
	if isinstance(configured, dict):
		return {company: tuple(roots or ()) for company, roots in configured.items()}
	return FAMILIES


def allowed_companies(user=None):
	"""None = unrestricted (Administrator, a Kit POS device session, a user with no Company User Permission that applies
	to all doctypes, Item Price or Price List); else the set of the user's companies. Cached per user per request."""
	user = user or frappe.session.user
	if user == "Administrator":
		return None
	if user == frappe.session.user and getattr(frappe.local, "kit_pos_device", None):
		return None
	cache = _memo().setdefault("companies", {})
	if user not in cache:
		rows = frappe.permissions.get_user_permissions(user).get("Company") or []
		allowed = {row.get("doc") for row in rows if (row.get("applicable_for") or "") in APPLICABLE_FOR}
		cache[user] = allowed or None
	return cache[user]


def is_privileged(user=None):
	"""Administrator, or a System Manager without a company restriction."""
	user = user or frappe.session.user
	return user == "Administrator" or (
		"System Manager" in frappe.get_roles(user) and allowed_companies(user) is None
	)


def company_default_list(company):
	"""The company's default selling list (Company.custom_default_selling_price_list) when it is an enabled selling list."""
	if not company:
		return None
	defaults = _memo().setdefault("defaults", {})
	if company not in defaults:
		value = None
		if frappe.get_meta("Company").has_field(DEFAULT_FIELD):
			value = frappe.db.get_value("Company", company, DEFAULT_FIELD)
		info = list_info(value)
		defaults[company] = value if info and info.enabled and info.selling else None
	return defaults[company]


# -- item families ----------------------------------------------------------------------------------------------------


def _root_bounds():
	"""{root item group: (lft, rgt)} for every root named in families() that exists."""
	memo = _memo()
	if "roots" not in memo:
		names = sorted({root for roots in families().values() for root in roots})
		memo["roots"] = {
			row.name: (row.lft, row.rgt)
			for row in frappe.get_all("Item Group", filters={"name": ["in", names or [""]]}, fields=["name", "lft", "rgt"])
		}
	return memo["roots"]


def item_roots(item_code):
	"""The family roots whose subtree holds the item's Item Group."""
	cache = _memo().setdefault("items", {})
	if item_code not in cache:
		group = frappe.db.get_value("Item", item_code, "item_group") if item_code else None
		bounds = frappe.db.get_value("Item Group", group, ["lft", "rgt"]) if group else None
		cache[item_code] = (
			{root for root, (lft, rgt) in _root_bounds().items() if lft <= bounds[0] and bounds[1] <= rgt}
			if bounds
			else set()
		)
	return cache[item_code]


def item_companies(item_code):
	"""Companies whose family holds the item (empty: the item is in no company's family)."""
	roots = item_roots(item_code)
	return {company for company, company_roots in families().items() if roots & set(company_roots)}


def family_allows(item_code, company):
	"""The item-family allowlist of a company list: True when the company has no family or the item is in it."""
	roots = families().get(company)
	if roots is None:
		return True
	return bool(item_roots(item_code) & set(roots))


# -- Item Price permissions ---------------------------------------------------------------------------------------------


def row_visible(user, price_list, item_code):
	"""May a restricted `user` see/change the Item Price of `item_code` in `price_list`? (split active)"""
	allowed = allowed_companies(user)
	if allowed is None:
		return True
	info = list_info(price_list)
	if not info:
		return True  # not a list (yet): ERPNext's own validation refuses it
	if not info.enabled:
		return False
	if info.company:
		return info.company in allowed
	owners = item_companies(item_code)
	if owners:
		return bool(owners & allowed)
	# an item of no family: only users of a company outside the families see it
	return bool(allowed - set(families()))


def item_price_has_permission(doc, ptype=None, user=None, debug=False):
	"""has_permission hook (explicit True/False). A write/delete checks the stored row as well as the new values, so a
	row cannot be moved out of (or into) a list the user may not use."""
	if not split_active():
		return True
	user = user or frappe.session.user
	if allowed_companies(user) is None:
		return True
	if doc.get("price_list") and not row_visible(user, doc.get("price_list"), doc.get("item_code")):
		return False
	if (ptype or "read") not in (*READ_PTYPES, "create") and doc.get("name"):
		stored = frappe.db.get_value("Item Price", doc.name, ["price_list", "item_code"], as_dict=True)
		if stored and not row_visible(user, stored.price_list, stored.item_code):
			return False
	return True


def item_price_query_conditions(user=None, doctype=None):
	"""permission_query_conditions hook: the same rule as item_price_has_permission, as SQL."""
	if not split_active():
		return ""
	user = user or frappe.session.user
	allowed = allowed_companies(user)
	if allowed is None:
		return ""
	own = sorted(n for n, i in lists().items() if i.enabled and i.company and i.company in allowed)
	shared = sorted(n for n, i in lists().items() if i.enabled and not i.company)
	conditions = []
	if own:
		conditions.append(f"`tabItem Price`.`price_list` in ({_sql_in(own)})")
	if shared:
		bounds = _root_bounds()
		mine = sorted({root for company in allowed for root in families().get(company, ()) if root in bounds})
		groups = [f"(ig.lft >= {cint(bounds[r][0])} and ig.rgt <= {cint(bounds[r][1])})" for r in mine]
		if allowed - set(families()):
			every = [f"(ig.lft >= {cint(lft)} and ig.rgt <= {cint(rgt)})" for lft, rgt in bounds.values()]
			groups.append(f"not ({' or '.join(every)})" if every else "1=1")
		if groups:
			conditions.append(
				f"(`tabItem Price`.`price_list` in ({_sql_in(shared)}) and exists (select 1 from `tabItem` it"
				" join `tabItem Group` ig on ig.name = it.item_group where it.name = `tabItem Price`.`item_code`"
				f" and ({' or '.join(groups)})))"
			)
	return "(" + " or ".join(conditions) + ")" if conditions else "1=0"


def _sql_in(values):
	return ", ".join(frappe.db.escape(v) for v in values)


# -- guards -------------------------------------------------------------------------------------------------------------


def validate_item_price(doc, method=None):
	"""Item-family allowlist of company lists, for every writer (also ignore_permissions inserts)."""
	if not split_active() or not cint(frappe.conf.get(GUARD_CONFIG, 1)):
		return
	info = list_info(doc.price_list)
	if not info or not info.company or family_allows(doc.item_code, info.company):
		return
	frappe.throw(family_message(doc.item_code, doc.price_list), ItemFamilyError, title=_("Wrong price list"))


def family_message(item_code, price_list):
	company = list_info(price_list).company
	roots = families().get(company) or ()
	return _("{0} (Item Group {1}) cannot be priced in {2}: that list takes only items of {3}.").format(
		item_code,
		frappe.db.get_value("Item", item_code, "item_group") or "-",
		price_list,
		_(" or ").join(roots),
	)


def validate_price_list(doc, method=None):
	if not split_active():
		return
	if doc.is_new():
		if cint(doc.selling) and not doc.get(FIELD):
			frappe.throw(_("A new selling price list needs a company."))
		allowed = allowed_companies()
		if allowed is not None and doc.get(FIELD) and doc.get(FIELD) not in allowed:
			frappe.throw(_("You may not create a price list for {0}.").format(doc.get(FIELD)), frappe.PermissionError)
		return
	before = doc.get_doc_before_save()
	if not before:
		return
	changed = [f for f in GUARDED_FIELDS if _norm(before.get(f), f) != _norm(doc.get(f), f)]
	if changed and not is_privileged():
		frappe.throw(
			_("Only an administrator may change {0} of price list {1}.").format(
				", ".join(doc.meta.get_label(f) for f in changed), doc.name
			),
			frappe.PermissionError,
		)
	if cint(before.enabled) and not cint(doc.enabled):
		check_not_in_use(doc.name)


def _norm(value, fieldname=None):
	if fieldname in CHECK_FIELDS:
		return cint(value)
	return "" if value is None else str(value)


def check_not_in_use(price_list):
	"""A list stays enabled while an active Kit POS Location or an enabled POS Profile sells with it."""
	users = []
	if frappe.db.table_exists("Kit POS Location"):
		users += [
			_("Kit POS Location {0}").format(n)
			for n in frappe.get_all(
				"Kit POS Location", filters={"selling_price_list": price_list, "active": 1}, pluck="name"
			)
		]
	users += [
		_("POS Profile {0}").format(n)
		for n in frappe.get_all("POS Profile", filters={"selling_price_list": price_list, "disabled": 0}, pluck="name")
	]
	if users:
		frappe.throw(
			_("Price list {0} cannot be disabled while it is used by {1}.").format(price_list, ", ".join(users))
		)


def before_rename_price_list(doc, method=None, old=None, new=None, merge=False):
	if split_active() and not is_privileged():
		frappe.throw(_("Only an administrator may rename a price list."), frappe.PermissionError)


def validate_consumer_list(doc, method=None):
	"""Kit POS Location (active) and POS Profile (enabled): an enabled list of their own company or of no company."""
	if not split_active() or not doc.get("selling_price_list"):
		return
	if (doc.doctype == "Kit POS Location" and not cint(doc.get("active"))) or cint(doc.get("disabled")):
		return
	info = list_info(doc.selling_price_list)
	if not info or not info.enabled:
		frappe.throw(_("Price list {0} is disabled: choose an enabled list.").format(doc.selling_price_list))
	if info.company and info.company != doc.get("company"):
		frappe.throw(
			_("{0} {1} sells for {2}: it cannot use price list {3} of {4}.").format(
				_(doc.doctype), doc.name, doc.get("company") or "-", doc.selling_price_list, info.company
			)
		)


def validate_party_default_list(doc, method=None):
	"""Customer, Customer Group, Supplier, Supplier Group. A party's list overrides the till's / the document's list on its
	invoices, so a new default is never a disabled list, and a restricted user sets only a list of their companies
	(suppliers: also a list of no company, the shared buying lists)."""
	if not split_active() or not doc.meta.has_field("default_price_list"):
		return
	value = doc.get("default_price_list")
	before = None if doc.is_new() else doc.get_doc_before_save()
	if not value or (before and before.get("default_price_list") == value):
		return
	info = list_info(value)
	if info and not info.enabled:
		frappe.throw(_("Price list {0} is disabled: it cannot be a default.").format(value))
	allowed = allowed_companies()
	if allowed is None:
		return
	shared_ok = doc.doctype in ("Supplier", "Supplier Group")
	if not info or (info.company not in allowed and not (shared_ok and not info.company)):
		frappe.throw(
			_("You may not set price list {0} as the default of {1} {2}.").format(value, _(doc.doctype), doc.name),
			frappe.PermissionError,
		)


# -- documents and items ------------------------------------------------------------------------------------------------


def selling_list_for(price_list, company, currency=None):
	"""The list a selling document (new, amended, returned or copied) should use instead of `price_list`: the same one
	when it is enabled, else the company's default list (same currency), else unchanged."""
	if not price_list or not split_active():
		return price_list
	info = list_info(price_list)
	if info and info.enabled:
		return price_list
	default = company_default_list(company)
	if not default:
		return price_list
	if currency and lists()[default].currency != currency:
		return price_list
	return default


def replace_disabled_selling_list(doc, method=None):
	"""before_validate of selling documents: a disabled header list becomes the company's default list. Lines keep
	their rates (ERPNext fills only missing line values), so nothing copied from a source document is re-priced, and
	a missing price is auto-inserted into the enabled list, never into the archive."""
	price_list = doc.get("selling_price_list")
	replacement = selling_list_for(price_list, doc.get("company"), doc.get("price_list_currency"))
	if replacement != price_list:
		doc.selling_price_list = replacement


@frappe.whitelist()
def selling_list_replacement(company, price_list):
	"""For the form script: the list a new document should use instead of the disabled `price_list` (or None)."""
	allowed = allowed_companies()
	if allowed is not None and company not in allowed:
		return None
	replacement = selling_list_for(price_list, company)
	return replacement if replacement != price_list else None


def fill_item_default_price_lists(doc, method=None):
	"""A new Item's Item Default rows without a list get their company's default list: Kit POS quick-add then writes
	only that list, and ERPNext's Standard Selling Rate price goes to it (not to the Selling Settings list)."""
	if not doc.is_new() or not split_active():
		return
	for row in doc.get("item_defaults") or []:
		if row.company and not row.default_price_list:
			row.default_price_list = company_default_list(row.company)


# -- ERPNext's automatic price writes -------------------------------------------------------------------------------------


def automatic_price_refused(price_list, item_code, user=None):
	"""An automatic price write into `price_list` is skipped (split active): the list is disabled; it belongs to a
	company and the item is outside that company's family (guard on); or the user who writes it (ERPNext inserts as the
	session user, after a doctype-level check only) may not use that row, which would fail the whole document with a
	PermissionError. The wrappers live in the process, which also serves the bench's other sites: only a site with this
	app is judged."""
	if not price_list or "fours_customizations" not in frappe.get_installed_apps() or not split_active():
		return False
	info = list_info(price_list)
	if not info:
		return False
	if not info.enabled:
		return True
	if info.company and cint(frappe.conf.get(GUARD_CONFIG, 1)) and not family_allows(item_code, info.company):
		return True
	user = user or frappe.session.user
	if not row_visible(user, price_list, item_code):
		noted = _memo().setdefault("skipped", set())
		if (user, price_list, item_code) not in noted:
			noted.add((user, price_list, item_code))
			frappe.logger("fours_price_lists").info(
				f"automatic price of {item_code} in {price_list} skipped: not a row of the user's companies"
			)
		return True
	return False


# The wrapped functions as ERPNext (v16, multax) defines them: insert_item_price(ctx), Item.add_price(self, price_list=None).
EXPECTED_SIGNATURES = {"insert_item_price": ("ctx",), "Item.add_price": ("self", "price_list")}
_signature_warned = []


def signatures_match(insert_item_price, add_price):
	"""ERPNext's functions still take the parameters the wrappers pass on (an upgrade may change them)."""
	import inspect

	try:
		got = {
			"insert_item_price": tuple(inspect.signature(insert_item_price).parameters),
			"Item.add_price": tuple(inspect.signature(add_price).parameters),
		}
	except (TypeError, ValueError):
		return False
	return got == EXPECTED_SIGNATURES


def patch_erpnext(*args, **kwargs):
	"""Wrap erpnext.stock.get_item_details.insert_item_price (called by name while a document's lines are priced) and
	Item.add_price (a new Item's Standard Selling Rate) with automatic_price_refused. Idempotent, per process. When
	ERPNext's signatures differ from EXPECTED_SIGNATURES, nothing is wrapped and one warning is logged per process."""
	from erpnext.stock import get_item_details
	from erpnext.stock.doctype.item.item import Item

	insert_done = getattr(get_item_details.insert_item_price, "fours_price_lists", False)
	add_done = getattr(Item.add_price, "fours_price_lists", False)
	if insert_done and add_done:
		return
	if not signatures_match(
		getattr(get_item_details.insert_item_price, "__wrapped__", get_item_details.insert_item_price),
		getattr(Item.add_price, "__wrapped__", Item.add_price),
	):
		if not _signature_warned:
			_signature_warned.append(True)
			frappe.logger("fours_price_lists").warning(
				"ERPNext insert_item_price / Item.add_price signatures changed: automatic price writes are NOT guarded "
				"by the price list split (fours_customizations.price_lists.patch_erpnext)"
			)
		return

	if not insert_done:
		original_insert = get_item_details.insert_item_price

		def insert_item_price(ctx):
			if automatic_price_refused(ctx.get("price_list"), ctx.get("item_code")):
				return
			return original_insert(ctx)

		insert_item_price.fours_price_lists = True
		insert_item_price.__wrapped__ = original_insert
		get_item_details.insert_item_price = insert_item_price

	if not add_done:
		original_add = Item.add_price

		def add_price(self, price_list=None):
			target = price_list or (
				"fours_customizations" in frappe.get_installed_apps()
				and (
					frappe.db.get_single_value("Selling Settings", "selling_price_list")
					or frappe.db.get_value("Price List", _("Standard Selling"))
				)
			)
			if automatic_price_refused(target, self.name):
				return
			return original_add(self, price_list)

		add_price.fours_price_lists = True
		add_price.__wrapped__ = original_add
		Item.add_price = add_price


patch_erpnext()

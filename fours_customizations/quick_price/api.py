"""Quick Price Update page API (fqp-1), ported from Jamissang's jm-price-update without Jamissang's price control.

The page (fours_customizations/fours_customizations/page/quick_price_update) lists, for a company chosen IN THE PAGE
(never the user's default company), the items the company sells or stocks with the price a new Sales Invoice would
fetch from each of the company's selling price lists, side by side, plus any enabled selling list the user adds.

Rules:
- Items, prices, stock and usage are read with plain queries plus explicit checks here (no Item permission query):
  the page's roles, Item Price read (a role with full permlevel-0 read), Company and Price List User Permissions.
- Every write goes through Item Price doc.save()/insert() WITHOUT ignore_permissions, so Item Price permissions,
  validations and Version tracking apply. Only the audit row (Quick Price Change) is inserted by this code.
- The editable state shown per cell is a dry run of Item Price write (existing record) or create (new record).
- A price list is shared by every company that sells with it: the page says so per list and per item.
"""

import json

import frappe
from erpnext.stock.get_item_details import get_item_price
from frappe import _
from frappe.utils import add_days, cint, flt, get_fullname, now_datetime, nowdate

PAGE = "quick-price-update"
SOURCE = "Quick Price Update"
LOG_DOCTYPE = "Quick Price Change"
EXCLUDED_ROLES = ("Kit TV Viewer",)  # Item Price read for the TV screens only: no page
MAX_PAGE_LENGTH = 100
MAX_CHANGES = 100
MAX_RATE = 1e12
LARGE_CHANGE_PCT = 30
LIST_USAGE_DAYS = 180  # a company uses the selling lists its submitted invoices used in this many days
ITEM_USAGE_DAYS = 365  # a company sells the items on its submitted invoices of this many days
CACHE_TTL = 600
CACHE_PREFIX = "fours_quick_price"


class PriceChangedError(frappe.ValidationError):
	pass


class LargeChangeError(frappe.ValidationError):
	pass


# -- whitelisted ----------------------------------------------------------------------------------------------------


@frappe.whitelist()
def get_context():
	"""Companies the user may use on the page, the enabled selling lists for the picker; `message` when none."""
	check_access()
	companies = accessible_companies(frappe.session.user)
	return {
		"companies": companies,
		"default_company": default_company(companies),
		"selling_lists": selectable_lists(frappe.session.user),
		"max_changes": MAX_CHANGES,
		"large_change_pct": LARGE_CHANGE_PCT,
		"page_length": MAX_PAGE_LENGTH,
		"message": None if companies else _("You cannot access any company."),
	}


@frappe.whitelist()
def get_lists(company, lists=None):
	"""The company's lists (+ the user's added `lists`) with currency and which companies use each one."""
	ctx = require_company(company, parse_list(lists))
	return {"company": ctx.company, "lists": ctx.list_info, "warehouses": ctx.warehouses}


@frappe.whitelist()
def get_items(
	company,
	lists=None,
	search=None,
	item_group=None,
	only_unpriced=0,
	all_sales_items=0,
	start=0,
	page_length=MAX_PAGE_LENGTH,
):
	"""One page of items with the effective price of each list (what a new Sales Invoice fetches today,
	customer-independent), stock per company warehouse, other companies selling it, and per-cell editability."""
	ctx = require_company(company, parse_list(lists))
	start = max(cint(start), 0)
	page_length = min(max(cint(page_length) or MAX_PAGE_LENGTH, 1), MAX_PAGE_LENGTH)
	rows = query_items(
		ctx, search, item_group, cint(only_unpriced), cint(all_sales_items), start, page_length + 1
	)
	has_more = len(rows) > page_length
	rows = rows[:page_length]

	codes = [row.item_code for row in rows]
	factors = sales_uom_factors(rows)
	duplicates = duplicate_cells(codes, ctx.lists)
	stock = stock_by_warehouse(codes, ctx.warehouses)
	others = other_sellers(ctx, codes)
	today = nowdate()

	cells = {}
	for row in rows:
		uom = row.sales_uom or row.stock_uom
		factor = factors.get((row.item_code, uom), 1.0)
		for price_list in ctx.lists:
			cells[(row.item_code, price_list)] = effective_price(
				row.item_code, price_list, uom, row.stock_uom, factor, today
			)
	records = price_records({cell["item_price"] for cell in cells.values() if cell.get("item_price")})
	can_create = frappe.has_permission("Item Price", "create")
	names = {}

	items = []
	for row in rows:
		uom = row.sales_uom or row.stock_uom
		item = {
			"item_code": row.item_code,
			"item_name": row.item_name,
			"item_group": row.item_group,
			"uom": uom,
			"stock_uom": row.stock_uom,
			"factor": factors.get((row.item_code, uom), 1.0),
			"stock": stock.get(row.item_code, {}),
			"also_sold_by": others.get(row.item_code, {}),
			"prices": {},
			"warnings": [],
			"last_change": None,
		}
		for price_list in ctx.lists:
			cell = cells[(row.item_code, price_list)]
			record = records.get(cell.get("item_price"))
			if record:
				if record.modified_by not in names:
					names[record.modified_by] = get_fullname(record.modified_by)
				cell.update(last_changed=str(record.modified), last_changed_by=names[record.modified_by])
				if not item["last_change"] or str(record.modified) > item["last_change"]["at"]:
					item["last_change"] = {"at": str(record.modified), "by": names[record.modified_by]}
			cell["duplicate"] = int((row.item_code, price_list) in duplicates)
			cell.update(cell_access(row.item_code, price_list, cell, record, can_create))
			if cell["duplicate"]:
				item["warnings"].append(
					_("{0} has more than one price for this item: clean up the duplicates first.").format(
						price_list
					)
				)
			item["prices"][price_list] = cell
		items.append(item)

	return {
		"items": items,
		"lists": ctx.list_info,
		"warehouses": [w for w in ctx.warehouses if any(w in s for s in stock.values())],
		"has_more": has_more,
		"start": start,
		"page_length": page_length,
	}


@frappe.whitelist(methods=["POST"])
def save_prices(company, changes, reason=None, confirm_large=0, lists=None):
	"""Apply price changes. `changes`: list (<= MAX_CHANGES) of {item_code, price_list, uom, item_price (name, or empty
	when the item has no price in that UOM yet), expected_old_rate, new_rate}. Each change runs in its own savepoint:
	one refused change never undoes the others. Returns one result per change."""
	ctx = require_company(company, parse_list(lists))
	if isinstance(changes, str):
		changes = json.loads(changes)
	if not isinstance(changes, list) or not changes:
		frappe.throw(_("No price changes were sent."))
	if len(changes) > MAX_CHANGES:
		frappe.throw(_("At most {0} price changes can be saved at once.").format(MAX_CHANGES))
	reason = (reason or "").strip()[:500]

	changes = [frappe._dict(change if isinstance(change, dict) else {}) for change in changes]
	results = [
		{"idx": idx, "item_code": c.item_code, "price_list": c.price_list} for idx, c in enumerate(changes, 1)
	]
	# Rows are locked in one order (item, list) by every save, so two saves of the page cannot deadlock each other.
	for i in sorted(
		range(len(changes)), key=lambda i: (str(changes[i].item_code), str(changes[i].price_list))
	):
		change, result = changes[i], results[i]
		messages = len(frappe.local.message_log)
		frappe.db.savepoint("fours_quick_price")
		try:
			result.update(save_one(ctx, change, reason, cint(confirm_large)))
			result["ok"] = 1
		except Exception as e:
			try:
				frappe.db.rollback(save_point="fours_quick_price")
				aborted = isinstance(e, frappe.QueryDeadlockError)
			except Exception:
				aborted = (
					True  # the database rolled the whole transaction back (deadlock): the savepoint is gone
				)
			if aborted:
				frappe.db.rollback()
				del frappe.local.message_log[messages:]
				return [
					dict(
						r,
						ok=0,
						error="Aborted",
						message=_(
							"Another save changed the same records at the same moment: nothing was saved. Try again."
						),
					)
					for r in results
				]
			result.update(ok=0, message=error_text(e, messages), error=e.__class__.__name__)
			# Refusals (validation, rights, concurrency) are normal use: only unexpected errors go to the Error Log.
			if not isinstance(e, frappe.ValidationError | frappe.PermissionError):
				frappe.log_error(f"Quick Price Update: {change.item_code} / {change.price_list}")
		finally:
			del frappe.local.message_log[messages:]
	return results


def save_one(ctx, change, reason, confirm_large):
	if change.price_list not in ctx.lists:
		frappe.throw(
			_("{0} is not a price list on this page for {1}.").format(change.price_list, ctx.company),
			frappe.PermissionError,
		)
	item = frappe.db.get_value(
		"Item",
		change.item_code or "",
		["name", "stock_uom", "sales_uom", "disabled", "has_variants", "is_sales_item"],
		as_dict=True,
	)
	if not item or item.disabled or item.has_variants or not item.is_sales_item:
		frappe.throw(_("{0} is not an enabled sales item.").format(change.item_code))
	uom = change.uom or item.stock_uom
	if uom != item.stock_uom and not frappe.db.exists(
		"UOM Conversion Detail", {"parent": item.name, "parenttype": "Item", "uom": uom}
	):
		frappe.throw(_("{0} is not a unit of {1}.").format(uom, item.name))
	new_rate = parse_rate(change.new_rate)

	# One writer per item at a time through this page, and no judging of a stale snapshot (assert_fresh).
	frappe.db.sql("select name from `tabItem` where name = %s for update", item.name)
	assert_fresh(item.name, change.price_list)
	today = nowdate()
	factor = uom_factor(item, uom)
	current = effective_price(item.name, change.price_list, uom, item.stock_uom, factor, today)

	doc = None
	if change.item_price:
		doc = frappe.get_doc("Item Price", change.item_price)
		if (
			(doc.item_code, doc.price_list, doc.uom or uom) != (item.name, change.price_list, uom)
			or doc.customer
			or doc.supplier
			or doc.batch_no
		):
			frappe.throw(
				_("Item Price {0} is not the {1} price of {2}.").format(
					doc.name, change.price_list, item.name
				)
			)
		# The record must still be the one a new Sales Invoice fetches: another user may have added a newer dated
		# price since the page loaded, and changing this record would then change nothing a sale sees.
		if current.get("item_price") != doc.name:
			raise_changed(item.name, change.price_list)
		old_rate = flt(doc.price_list_rate)
	else:
		if current.get("item_price"):
			raise_changed(item.name, change.price_list)
		old_rate = current.get("rate")
		doc = frappe.new_doc("Item Price")
		doc.update({"item_code": item.name, "price_list": change.price_list, "uom": uom, "valid_from": today})

	# Rights first (the same answer whatever else is wrong with the change); doc.save()/insert() check them again.
	ptype = "create" if doc.is_new() else "write"
	if not frappe.has_permission("Item Price", ptype, doc=doc):
		frappe.throw(
			_("You may not {0} the {1} price of {2}.").format(
				_("create") if ptype == "create" else _("change"), change.price_list, item.name
			),
			frappe.PermissionError,
		)
	if (item.name, change.price_list) in duplicate_cells([item.name], [change.price_list]):
		frappe.throw(
			_("{0} has more than one {1} price: clean up the duplicates first.").format(
				item.name, change.price_list
			)
		)

	precision = rate_precision()
	if flt(change.get("expected_old_rate") or 0, precision) != flt(old_rate or 0, precision):
		raise_changed(item.name, change.price_list)
	if not doc.is_new() and flt(new_rate, precision) == flt(old_rate, precision):
		return {"status": "unchanged", "new_rate": new_rate, "old_rate": old_rate, "item_price": doc.name}
	if is_large_change(old_rate, new_rate) and not confirm_large:
		frappe.throw(
			_(
				"{0} ({1}): {2} to {3} changes the price by more than {4}%. Confirm the large change to save it."
			).format(item.name, change.price_list, fmt(old_rate), fmt(new_rate), LARGE_CHANGE_PCT),
			LargeChangeError,
		)

	status = "created" if doc.is_new() else "changed"
	doc.price_list_rate = new_rate
	if doc.is_new():
		doc.insert()
	else:
		doc.save()
	log = frappe.get_doc(
		{
			"doctype": LOG_DOCTYPE,
			"company": ctx.company,
			"item_code": item.name,
			"price_list": change.price_list,
			"uom": uom,
			"action": "Changed" if status == "changed" else "Created",
			"old_rate": old_rate if status == "changed" else 0,
			"new_rate": flt(doc.price_list_rate),
			"item_price": doc.name,
			"user": frappe.session.user,
			"reason": reason or None,
			"timestamp": now_datetime(),
			"source": SOURCE,
			"shared_with": ", ".join(c for c in ctx.used_by.get(change.price_list, []) if c != ctx.company)
			or None,
		}
	)
	log.flags.ignore_permissions = True  # nobody has create on the log: only this API writes it
	log.flags.from_quick_price_update = True
	log.insert()
	return {
		"status": status,
		"new_rate": flt(doc.price_list_rate),
		"old_rate": old_rate,
		"item_price": doc.name,
		"log": log.name,
	}


# -- access ---------------------------------------------------------------------------------------------------------


def price_read_roles():
	"""Roles with full (permlevel 0, not only-own) read on Item Price, from the effective rules: the site's Custom
	DocPerm rows when there are any for Item Price (they replace the standard ones), else the standard DocPerm rows."""
	rules = frappe.get_all(
		"Custom DocPerm",
		filters={"parent": "Item Price"},
		fields=["role", "read", "permlevel", "if_owner"],
	) or frappe.get_all(
		"DocPerm",
		filters={"parent": "Item Price", "parenttype": "DocType"},
		fields=["role", "read", "permlevel", "if_owner"],
	)
	return sorted({r.role for r in rules if cint(r.read) and not cint(r.permlevel) and not cint(r.if_owner)})


def page_roles():
	"""The page's roles: every role with Item Price read except EXCLUDED_ROLES, plus System Manager."""
	return sorted({r for r in price_read_roles() if r not in EXCLUDED_ROLES} | {"System Manager"})


def page_permitted():
	"""The page's roles (Page Has Role + Custom Role) for the session user; no message."""
	if not frappe.db.exists("Page", PAGE):
		return False
	return bool(frappe.get_doc("Page", PAGE).is_permitted())


def can_read_prices(user=None):
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(frappe.get_roles(user)) & set(price_read_roles()))


def check_access():
	"""The page's roles also gate the API, and no data without Item Price read."""
	if not page_permitted():
		frappe.throw(_("You are not permitted to use Quick Price Update."), frappe.PermissionError)
	if not can_read_prices():
		frappe.throw(_("You are not permitted to read Item Prices."), frappe.PermissionError)


def user_permission_values(user, doctype, applies_to="Item Price"):
	"""Desk semantics: None = no restriction (Administrator, or no User Permission on `doctype`); otherwise the set of
	the user's `doctype` User Permissions. For Company every entry counts (the page shows company data whatever the
	entry applies to); for Price List the entries that apply to all doctypes or to Item Price."""
	if user == "Administrator":
		return None
	entries = frappe.permissions.get_user_permissions(user).get(doctype) or []
	values = {
		entry.get("doc")
		for entry in entries
		if applies_to is None or not entry.get("applicable_for") or entry.get("applicable_for") == applies_to
	}
	return values or None


def accessible_companies(user):
	allowed = user_permission_values(user, "Company", applies_to=None)
	return [
		c
		for c in frappe.get_all("Company", pluck="name", order_by="name asc")
		if allowed is None or c in allowed
	]


def default_company(companies):
	preferred = frappe.defaults.get_user_default("Company")
	return preferred if preferred in companies else (companies[0] if companies else None)


def selectable_lists(user):
	"""Enabled selling price lists the user may see (Price List User Permissions)."""
	allowed = user_permission_values(user, "Price List")
	return [
		row
		for row in frappe.get_all(
			"Price List",
			filters={"enabled": 1, "selling": 1},
			fields=["name", "currency"],
			order_by="name asc",
		)
		if allowed is None or row.name in allowed
	]


def require_company(company, extra_lists=None):
	"""The page context for `company`, or PermissionError when the user may not use the page or the company."""
	check_access()
	user = frappe.session.user
	if not company or company not in accessible_companies(user):
		frappe.throw(_("You cannot access {0}.").format(company or _("this company")), frappe.PermissionError)
	return company_context(company, extra_lists or [], user)


def company_context(company, extra_lists, user):
	usage = list_usage()
	selectable = {row.name: row.currency for row in selectable_lists(user)}
	own = company_lists(company, usage)
	lists = []
	for name in own + list(extra_lists):
		if name in selectable and name not in lists:
			lists.append(name)
	used_by = {name: users_of(name, usage) for name in lists}
	info = []
	for name in lists:
		users = used_by[name]
		info.append(
			{
				"price_list": name,
				"currency": selectable[name],
				"own": int(name in own),
				"used_by": users,
				"notice": shared_notice(name, users, company),
			}
		)
	return frappe._dict(
		company=company,
		allowed_items=user_permission_values(user, "Item"),
		allowed_brands=user_permission_values(user, "Brand"),
		lists=lists,
		list_info=info,
		used_by=used_by,
		usage=usage,
		warehouses=company_warehouses(company),
	)


# -- lists and companies ----------------------------------------------------------------------------------------------


def list_usage():
	"""{company: {price list: number of submitted Sales/POS Invoices in LIST_USAGE_DAYS}}; an enabled POS Profile's list
	counts as used too (0 invoices). Cached CACHE_TTL seconds (the page shows usage, not money)."""
	key = f"{CACHE_PREFIX}:list_usage"
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return cached
	since = add_days(nowdate(), -LIST_USAGE_DAYS)
	usage = {}
	for doctype in ("Sales Invoice", "POS Invoice"):
		if not frappe.db.table_exists(doctype):
			continue
		for company, price_list, count in frappe.db.sql(
			f"""select company, selling_price_list, count(*) from `tab{doctype}`
			where docstatus = 1 and posting_date >= %s and ifnull(selling_price_list, '') != ''
			group by company, selling_price_list""",
			since,
		):
			lists = usage.setdefault(company, {})
			lists[price_list] = lists.get(price_list, 0) + cint(count)
	for row in frappe.get_all(
		"POS Profile", filters={"disabled": 0}, fields=["company", "selling_price_list"]
	):
		if row.selling_price_list:
			usage.setdefault(row.company, {}).setdefault(row.selling_price_list, 0)
	frappe.cache.set_value(key, usage, expires_in_sec=CACHE_TTL)
	return usage


def company_lists(company, usage):
	"""The company's own lists: those its invoices used (most used first) and its POS Profiles', then the site's
	default selling list (Selling Settings) when it is not among them."""
	used = usage.get(company, {})
	lists = sorted(used, key=lambda name: (-used[name], name))
	default = frappe.db.get_single_value("Selling Settings", "selling_price_list")
	if default and default not in lists:
		lists.append(default)
	return lists


def users_of(price_list, usage):
	"""Companies that sell with `price_list` (invoices in LIST_USAGE_DAYS or an enabled POS Profile), by name."""
	return sorted(company for company, lists in usage.items() if price_list in lists)


def shared_notice(price_list, users, company):
	others = [c for c in users if c != company]
	if not others:
		return None
	if len(users) == 1:
		return _("{0} is used by {1}: a change applies to it too.").format(price_list, users[0])
	return _("{0} is used by {1}: a change applies to all of them.").format(price_list, join_names(users))


def join_names(names):
	names = list(names)
	if len(names) < 2:
		return "".join(names)
	return _("{0} and {1}").format(", ".join(names[:-1]), names[-1])


def company_warehouses(company):
	return frappe.get_all(
		"Warehouse", filters={"company": company, "is_group": 0}, pluck="name", order_by="name asc"
	)


def company_items(company):
	"""Item codes the company sells or stocks: Item Defaults for the company, submitted Sales/POS Invoice lines of
	ITEM_USAGE_DAYS, Bins in its warehouses. Cached CACHE_TTL seconds."""
	key = f"{CACHE_PREFIX}:items:{company}"
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return set(cached)
	since = add_days(nowdate(), -ITEM_USAGE_DAYS)
	codes = set(
		frappe.db.sql_list(
			"select distinct parent from `tabItem Default` where parenttype = 'Item' and company = %s",
			company,
		)
	)
	for doctype in ("Sales Invoice", "POS Invoice"):
		if not frappe.db.table_exists(doctype):
			continue
		codes.update(
			frappe.db.sql_list(
				f"""select distinct line.item_code from `tab{doctype} Item` line
				join `tab{doctype}` inv on inv.name = line.parent
				where inv.company = %s and inv.docstatus = 1 and inv.posting_date >= %s""",
				(company, since),
			)
		)
	codes.update(
		frappe.db.sql_list(
			"""select distinct bin.item_code from `tabBin` bin
			join `tabWarehouse` wh on wh.name = bin.warehouse where wh.company = %s""",
			company,
		)
	)
	codes.discard(None)
	frappe.cache.set_value(key, sorted(codes), expires_in_sec=CACHE_TTL)
	return codes


def other_sellers(ctx, codes):
	"""{item_code: {price list: [other companies using that list that sell or stock the item]}}."""
	out = {}
	if not codes:
		return out
	sets = {}
	for price_list in ctx.lists:
		for company in ctx.used_by.get(price_list, []):
			if company == ctx.company:
				continue
			if company not in sets:
				sets[company] = company_items(company)
			for code in codes:
				if code in sets[company]:
					out.setdefault(code, {}).setdefault(price_list, []).append(company)
	return out


def clear_cache():
	"""Forget cached list usage and company item sets (tests; after big data changes)."""
	frappe.cache.delete_keys(f"{CACHE_PREFIX}:")


# -- items and prices ---------------------------------------------------------------------------------------------------


def query_items(ctx, search, item_group, only_unpriced, all_sales_items, start, limit):
	"""Enabled, non-template sales items of the company (or every sales item with all_sales_items), filtered and paged;
	never through the Item permission query."""
	from frappe.query_builder.functions import IfNull
	from pypika.terms import ExistsCriterion

	item = frappe.qb.DocType("Item")
	price = frappe.qb.DocType("Item Price")
	barcode = frappe.qb.DocType("Item Barcode")
	today = nowdate()

	query = (
		frappe.qb.from_(item)
		.select(item.name.as_("item_code"), item.item_name, item.item_group, item.stock_uom, item.sales_uom)
		.where((item.disabled == 0) & (item.has_variants == 0) & (item.is_sales_item == 1))
	)
	# Item Price read limited by Item / Brand User Permissions: only those items' prices
	if ctx.get("allowed_items") is not None:
		query = query.where(item.name.isin(sorted(ctx.allowed_items) or [""]))
	if ctx.get("allowed_brands") is not None:
		query = query.where(
			(IfNull(item.brand, "") == "") | item.brand.isin(sorted(ctx.allowed_brands) or [""])
		)
	if not all_sales_items:
		codes = company_items(ctx.company)
		if not codes:
			return []
		query = query.where(item.name.isin(sorted(codes)))
	search = (search or "").strip()
	if search:
		like = f"%{search}%"
		query = query.where(
			item.name.like(like)
			| item.item_name.like(like)
			| ExistsCriterion(
				frappe.qb.from_(barcode)
				.select(barcode.name)
				.where(
					(barcode.parent == item.name)
					& (barcode.parenttype == "Item")
					& barcode.barcode.like(like)
				)
			)
		)
	if item_group:
		bounds = frappe.db.get_value("Item Group", item_group, ["lft", "rgt"], as_dict=True)
		if not bounds:
			return []
		group = frappe.qb.DocType("Item Group")
		query = query.where(
			item.item_group.isin(
				frappe.qb.from_(group)
				.select(group.name)
				.where((group.lft >= bounds.lft) & (group.rgt <= bounds.rgt))
			)
		)
	if only_unpriced and ctx.lists:
		missing = None
		for price_list in ctx.lists:
			priced = ExistsCriterion(
				frappe.qb.from_(price)
				.select(price.name)
				.where(
					(price.item_code == item.name)
					& (price.price_list == price_list)
					& (IfNull(price.customer, "") == "")
					& (IfNull(price.supplier, "") == "")
					& (IfNull(price.valid_from, "2000-01-01") <= today)
					& (IfNull(price.valid_upto, "2500-12-31") >= today)
				)
			)
			missing = ~priced if missing is None else (missing | ~priced)
		query = query.where(missing)
	return query.orderby(item.item_name).orderby(item.name).limit(limit).offset(start).run(as_dict=True)


def effective_price(item_code, price_list, uom, stock_uom, factor, date):
	"""The generic (no customer) price a new Sales Invoice in `uom` fetches from `price_list` on `date`
	(erpnext.stock.get_item_details.get_price_list_rate_for): the `uom` price, else the stock-UOM price x factor.
	A derived price has no record in `uom` yet: saving the cell creates one."""
	pctx = {"price_list": price_list, "uom": uom, "transaction_date": date}
	found = get_item_price(pctx, item_code)
	if found:
		row = found[0]
		if row.uom == uom or (not row.uom and uom == stock_uom):
			return {"item_price": row.name, "rate": flt(row.price_list_rate), "record_uom": row.uom or uom}
		# a record without a unit is a stock-unit price: ERPNext multiplies it by the factor
		return {
			"item_price": None,
			"rate": flt(row.price_list_rate) * flt(factor or 1),
			"derived_from": row.name,
			"record_uom": uom,
		}
	if uom != stock_uom:
		found = get_item_price(dict(pctx, uom=stock_uom), item_code)
		if found:
			row = found[0]
			return {
				"item_price": None,
				"rate": flt(row.price_list_rate) * flt(factor or 1),
				"derived_from": row.name,
				"record_uom": uom,
			}
	return {"item_price": None, "rate": None, "record_uom": uom}


def price_records(names):
	"""The Item Price rows behind the cells, for the permission dry run and 'last change'."""
	if not names:
		return {}
	return {
		row.name: row
		for row in frappe.get_all(
			"Item Price",
			filters={"name": ["in", list(names)]},
			fields=["name", "item_code", "price_list", "uom", "owner", "modified", "modified_by"],
		)
	}


def cell_access(item_code, price_list, cell, record, can_create):
	"""Editable state of one cell for the session user (UX; save_prices checks everything again)."""
	if cell.get("duplicate"):
		return {
			"editable": 0,
			"lock_reason": _("More than one price in this list: clean up the duplicates first."),
		}
	if record:
		doc = frappe.get_doc({"doctype": "Item Price", **record})
		if not frappe.has_permission("Item Price", "write", doc=doc):
			return {"editable": 0, "lock_reason": _("You may not change this price.")}
		return {"editable": 1, "lock_reason": None}
	if not can_create:
		return {"editable": 0, "lock_reason": _("You may not create prices.")}
	doc = frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item_code,
			"price_list": price_list,
			"uom": cell.get("record_uom"),
		}
	)
	if not frappe.has_permission("Item Price", "create", doc=doc):
		return {"editable": 0, "lock_reason": _("You may not create prices in this list.")}
	return {"editable": 1, "lock_reason": None}


def duplicate_cells(codes, price_lists):
	"""(item_code, price_list) pairs with more than one generic row for the same UOM and validity: ERPNext refuses to
	save any of them (ItemPriceDuplicateItem) until the duplicates are cleaned up."""
	if not codes or not price_lists:
		return set()
	seen, dups = set(), set()
	for row in frappe.get_all(
		"Item Price",
		filters={"item_code": ["in", list(codes)], "price_list": ["in", list(price_lists)]},
		fields=[
			"item_code",
			"price_list",
			"uom",
			"valid_from",
			"valid_upto",
			"customer",
			"supplier",
			"batch_no",
		],
	):
		if row.customer or row.supplier:
			continue
		key = (
			row.item_code,
			row.price_list,
			row.uom or "",
			row.valid_from,
			row.valid_upto,
			row.batch_no or "",
		)
		if key in seen:
			dups.add((row.item_code, row.price_list))
		seen.add(key)
	return dups


def stock_by_warehouse(codes, warehouses):
	"""{item_code: {warehouse: actual qty (stock UOM)}} for the company's warehouses, non-zero only."""
	if not codes or not warehouses:
		return {}
	out = {}
	for row in frappe.get_all(
		"Bin",
		filters={"item_code": ["in", codes], "warehouse": ["in", warehouses], "actual_qty": ["!=", 0]},
		fields=["item_code", "warehouse", "actual_qty"],
	):
		out.setdefault(row.item_code, {})[row.warehouse] = flt(row.actual_qty)
	return out


def sales_uom_factors(rows):
	"""{(item_code, sales uom): conversion factor} for rows whose sales UOM differs from the stock UOM."""
	wanted = {
		(row.item_code, row.sales_uom) for row in rows if row.sales_uom and row.sales_uom != row.stock_uom
	}
	if not wanted:
		return {}
	found = frappe.get_all(
		"UOM Conversion Detail",
		filters={"parenttype": "Item", "parent": ["in", list({code for code, _uom in wanted})]},
		fields=["parent", "uom", "conversion_factor"],
	)
	return {(r.parent, r.uom): flt(r.conversion_factor) or 1.0 for r in found if (r.parent, r.uom) in wanted}


def uom_factor(item, uom):
	"""Conversion factor of `uom` for the item (1 for its stock UOM)."""
	return sales_uom_factors(
		[frappe._dict(item_code=item.name, sales_uom=uom, stock_uom=item.stock_uom)]
	).get((item.name, uom), 1.0)


# -- helpers ------------------------------------------------------------------------------------------------------------


def parse_list(value):
	if not value:
		return []
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			value = [value]
	return [str(v) for v in value if v][:20] if isinstance(value, list | tuple) else []


def rate_precision():
	return cint(frappe.get_precision("Item Price", "price_list_rate")) or 2


def parse_rate(value):
	try:
		rate = float(value)
	except (TypeError, ValueError):
		rate = 0
	if not rate > 0 or rate > MAX_RATE:
		frappe.throw(_("Enter a price above 0."))
	return flt(rate, rate_precision())


def is_large_change(old_rate, new_rate):
	# rounded: 1,000 -> 1,300 is exactly 30% (not 30.000000000000004%), which needs no confirmation
	return (
		bool(old_rate) and flt(abs(flt(new_rate) - flt(old_rate)) / flt(old_rate) * 100, 6) > LARGE_CHANGE_PCT
	)


def assert_fresh(item_code, price_list):
	"""The plain reads of this request (get_item_price, the duplicate checks, ERPNext's own) see the snapshot taken at its
	first read (REPEATABLE READ); a locking read sees what is committed now and locks it until the request ends. If the
	two differ, another user changed this item's prices in this list since that snapshot: refuse rather than judge (or
	duplicate) on stale data."""
	query = (
		"select name, modified, price_list_rate from `tabItem Price` where item_code = %s and price_list = %s"
	)
	committed = sorted(frappe.db.sql(query + " for update", (item_code, price_list)))
	if committed != sorted(frappe.db.sql(query, (item_code, price_list))):
		raise_changed(item_code, price_list)


def raise_changed(item_code, price_list):
	frappe.throw(
		_("The {0} price of {1} was changed by someone else. Refresh and try again.").format(
			price_list, item_code
		),
		PriceChangedError,
	)


def error_text(exc, first_message):
	"""The message the user saw for this change: the last msgprint since it began, else the exception text."""
	for message in reversed(frappe.local.message_log[first_message:]):
		try:
			text = json.loads(message).get("message") if isinstance(message, str) else message.get("message")
		except (ValueError, AttributeError):
			text = str(message)
		if text:
			return frappe.utils.strip_html(str(text))
	return frappe.utils.strip_html(str(exc)) or exc.__class__.__name__


def fmt(value):
	return f"{flt(value):,.2f}".rstrip("0").rstrip(".")

"""ps2.py - price list split PHASE 2 data step (multax.kit.africa), run right after the ps-fours-2 code release.

    su - frappe -c "cd /home/frappe/frappe-bench && PS_MODE=plan PS_TS=<stamp> bench --site multax.kit.africa console" \\
        <<< $'\\nexec(open("/home/frappe/price-split/kit/ps2.py").read(), globals())'

PS_MODE  plan (default, read-only) | apply | verify (read-only) | rollback
PS_VOX_ROWS  expected Vox Accountant Custom DocPerm rows for verify (default 48 = multax; the lab passes its own)
PS_TS    names the state directory /home/frappe/price-split/<PS_TS>/ (700; files 600): ps2-before.json (every value this
         step changes, as it was, with its sha256 in ps2-before.sha256) and ps2-log.txt. Use a new stamp for Phase 2 and
         the SAME stamp for every later apply / verify / rollback (a new stamp on an applied site stops).

apply (idempotent; each stage commits; a re-run skips what is done and stops on drift):
  1 gates   the release is live (price_lists hooks, Company default-list field); Phase 1 is in place (the three company
            lists exist, enabled, selling, UGX, of their company); no active Kit POS Location / enabled POS Profile, no
            Selling Settings value, global default, Customer / Customer Group / Supplier / Supplier Group default or Item
            Default row uses Standard Selling (SS), Selling Price Vox Nansana (SPVN) or the empty 'Standard' list
            (user defaults and drafts: reported only; for 'Standard' a user default also stops)
  2 defaults Company.custom_default_selling_price_list of the three companies -> their list
  3 archive  SS, SPVN and 'Standard' disabled through the release's guard (price_lists.check_not_in_use + the Price List
            rules), written with db.set_value so ERPNext's Price List.on_update does not re-stamp the archive's Item
            Prices; SS and 'Standard' keep a blank company, SPVN gets Vox Lounge Nansana ('Standard' missing on a site:
            nothing to do)
  4 page     the Quick Price page's Custom Role gets back its six roles (Phase 1 paused it to System Manager)
  5 caches   fours_quick_price redis keys, ERPNext's price_list_details of both archives, frappe cache of the site
verify (read-only): the values above; per enabled price reader with a Company row, the Item Price list query equals the
  rule and shows no row of another company's list or of a disabled list; consumers; counts of SS / SPVN prices; Vox
  Accountant Custom DocPerm rows = 48.
rollback: back from ps2-before.json in reverse (page roles, archives' enabled/company, Company defaults), caches; safe after
  a partial apply (only recorded stages are undone; a value changed since apply stops the rollback of that value).
  plan and rollback do not import the release's code: they also run after the code went back to fe153b5 (the kit's
  rollback refuses to swap back while the data step is applied; this is the way out if it was forced).
Prints names of lists, companies and roles, counts and 8-hex user hashes only.
"""

import hashlib
import json
import os

import frappe

SS, SPVN, STANDARD = "Standard Selling", "Selling Price Vox Nansana", "Standard"
FIELD = "custom_company"  # Price List -> Company (Phase 1's Custom Field) = price_lists.FIELD
DEFAULT_FIELD = "custom_default_selling_price_list"  # Company -> Price List (the release's field) = price_lists.DEFAULT_FIELD
LISTS = {  # Phase 1's company lists
	"4S Industries Limited": "4S Selling Price",
	"Vox Lounge": "Vox Makindye Selling Price",
	"Vox Lounge Nansana": "Vox Nansana Selling Price",
}
ARCHIVES = {SS: None, SPVN: "Vox Lounge Nansana", STANDARD: None}  # archive -> company it gets (None: stays blank)
OPTIONAL = (STANDARD,)  # archived when the site has it (multax: empty, enabled, no company)
PAGE = "quick-price-update"
PAGE_ROLES = (
	"Sales Master Manager",
	"4S Accountant",
	"Sales User",
	"System Manager",
	"Vox Accountant",
	"Purchase Master Manager",
)
VOX_ROWS = int(os.environ.get("PS_VOX_ROWS", "48"))  # Vox Accountant Custom DocPerm rows on multax (lab: its own count)
ROOT = "/home/frappe/price-split"


class Stop(Exception):
	pass


def ps2_main():
	mode = os.environ.get("PS_MODE", "plan")
	stamp = os.environ.get("PS_TS", "")
	if mode not in ("plan", "apply", "verify", "rollback"):
		raise Stop(f"PS_MODE {mode!r}: plan | apply | verify | rollback")
	if not stamp or not stamp.replace("-", "").replace("T", "").replace("Z", "").isalnum():
		raise Stop("set PS_TS to the Phase 2 stamp (names /home/frappe/price-split/<PS_TS>/)")
	state = os.path.join(ROOT, stamp)
	os.makedirs(state, mode=0o700, exist_ok=True)
	os.chmod(state, 0o700)
	ctx = frappe._dict(mode=mode, state=state, log=os.path.join(state, "ps2-log.txt"))
	frappe.set_user("Administrator")
	say(ctx, f"ps2 {mode} on {frappe.local.site} | state {state}")
	{"plan": plan, "apply": apply, "verify": verify, "rollback": rollback}[mode](ctx)


# -- io -------------------------------------------------------------------------------------------------------------------


def say(ctx, msg):
	print(msg, flush=True)
	fd = os.open(ctx.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
	with os.fdopen(fd, "a") as f:
		f.write(f"{frappe.utils.now()} {msg}\n")


def before_path(ctx):
	return os.path.join(ctx.state, "ps2-before.json")


def load_before(ctx):
	path = before_path(ctx)
	if not os.path.exists(path):
		return {}
	data = open(path, "rb").read()
	digest = open(path.replace(".json", ".sha256")).read().split()[0]
	if hashlib.sha256(data).hexdigest() != digest:
		raise Stop(f"{path} does not match its sha256: stop and look")
	return json.loads(data)


def save_before(ctx, before):
	data = json.dumps(before, indent=1, sort_keys=True, default=str).encode()
	for path, content in ((before_path(ctx), data), (before_path(ctx).replace(".json", ".sha256"), None)):
		fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
		with os.fdopen(fd, "wb") as f:
			f.write(content if content is not None else f"{hashlib.sha256(data).hexdigest()}  ps2-before.json\n".encode())
		os.replace(path + ".tmp", path)


def user_hash(user):
	return hashlib.sha1(user.encode()).hexdigest()[:8]


# -- facts ------------------------------------------------------------------------------------------------------------------


def clear_caches():
	"""price_lists.clear_caches() without importing the release (this request's memo + the Quick Price page's keys)."""
	frappe.local.fours_price_lists = {}
	frappe.cache.delete_keys("fours_quick_price:")


def read_lists():
	"""{name: {company, enabled, selling, currency}} of every Price List, read directly (= price_lists.lists())."""
	fields = ["name", "enabled", "selling", "currency"] + ([FIELD] if frappe.get_meta("Price List").has_field(FIELD) else [])
	cint = frappe.utils.cint
	return {
		r.name: frappe._dict(company=r.get(FIELD) or None, enabled=cint(r.enabled), selling=cint(r.selling), currency=r.currency)
		for r in frappe.get_all("Price List", fields=fields)
	}


def archives(lists):
	"""The archives this site has (an OPTIONAL one may be missing)."""
	return [n for n in ARCHIVES if n in lists or n not in OPTIONAL]


def facts():
	clear_caches()
	role = frappe.db.get_value("Custom Role", {"page": PAGE}, "name")
	has_default = frappe.get_meta("Company").has_field(DEFAULT_FIELD)
	return frappe._dict(
		lists=read_lists(),
		defaults={
			c: frappe.db.get_value("Company", c, DEFAULT_FIELD) if has_default else None
			for c in LISTS
			if frappe.db.exists("Company", c)
		},
		page_role=role,
		page_roles=sorted(frappe.get_all("Has Role", filters={"parenttype": "Custom Role", "parent": role}, pluck="role"))
		if role
		else [],
	)


def gates(ctx, strict=True):
	"""The release and Phase 1 are in place; nothing active still sells with an archive. Returns the problems."""
	problems = []
	hooks = frappe.get_hooks("has_permission") or {}
	if "fours_customizations.price_lists.item_price_has_permission" not in hooks.get("Item Price", []):
		problems.append("the ps-fours-2 release is not live (Item Price has_permission hook missing)")
	if not frappe.get_meta("Company").has_field(DEFAULT_FIELD):
		problems.append(f"Company.{DEFAULT_FIELD} missing (the release's migrate did not run)")
	if not frappe.get_meta("Price List").has_field(FIELD):
		problems.append("Price List.custom_company missing: Phase 1 has not run")
		return problems
	lists = read_lists()
	for company, name in LISTS.items():
		info = lists.get(name)
		if not info:
			problems.append(f"Phase 1 list {name} missing")
		elif (info.company, info.enabled, info.selling, info.currency) != (company, 1, 1, "UGX"):
			problems.append(f"{name}: company {info.company}, enabled {info.enabled}, selling {info.selling}, {info.currency}")
	for name in ARCHIVES:
		if name not in lists:
			if name in OPTIONAL:
				say(ctx, f"  {name}: not on this site, nothing to archive")
			else:
				problems.append(f"{name} does not exist")
	names = archives(lists)
	users = []
	if frappe.db.table_exists("Kit POS Location"):
		users += [
			f"Kit POS Location {r.name} ({r.selling_price_list})"
			for r in frappe.get_all(
				"Kit POS Location",
				filters={"active": 1, "selling_price_list": ["in", names]},
				fields=["name", "selling_price_list"],
			)
		]
	users += [
		f"POS Profile {r.name} ({r.selling_price_list})"
		for r in frappe.get_all(
			"POS Profile",
			filters={"disabled": 0, "selling_price_list": ["in", names]},
			fields=["name", "selling_price_list"],
		)
	]
	setting = frappe.db.get_single_value("Selling Settings", "selling_price_list")
	if setting in names:
		users.append(f"Selling Settings.selling_price_list ({setting})")
	if frappe.db.get_default("selling_price_list") in names:
		users.append(f"global default selling_price_list ({frappe.db.get_default('selling_price_list')})")
	# a party's default replaces a POS invoice's list after the release's before_validate; an Item Default list is
	# where a new item's price and a till quick-add go: none may point at an archive
	for doctype in ("Customer Group", "Customer", "Supplier", "Supplier Group"):
		n = frappe.db.count(doctype, {"default_price_list": ["in", names]})
		if n:
			users.append(f"{n} {doctype} default(s)")
	n = frappe.db.count("Item Default", {"default_price_list": ["in", names]})
	if n:
		users.append(f"{n} Item Default row(s)")
	# 'Standard' is not in use at all (lead, 2026-10-23): a user default of it also stops
	for name in OPTIONAL:
		if name in names:
			n = user_defaults([name])
			if n:
				users.append(f"{n} user default(s) of {name}")
	problems += [f"still sells with an archive: {u}" for u in users]
	return problems


def user_defaults(names):
	return frappe.db.sql(
		"select count(*) from `tabDefaultValue` where defkey in ('selling_price_list', 'price_list') and defvalue in %s"
		" and parent not in ('__default', '__global')",
		[names],
	)[0][0]


def references(ctx):
	"""Other references to the archives (reported; the release's code moves new documents off a disabled list)."""
	archives = list(ARCHIVES)
	rows = [
		("Customer Group defaults", frappe.db.count("Customer Group", {"default_price_list": ["in", archives]})),
		("Customer defaults", frappe.db.count("Customer", {"default_price_list": ["in", archives]})),
		("Item Default rows", frappe.db.count("Item Default", {"default_price_list": ["in", archives]})),
		("user defaults", user_defaults(archives)),
	]
	for doctype in ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "POS Invoice"):
		if frappe.db.table_exists(doctype):
			rows.append(
				(f"draft {doctype}", frappe.db.count(doctype, {"docstatus": 0, "selling_price_list": ["in", archives]}))
			)
	for label, count in rows:
		say(ctx, f"  references to the archives: {label}: {count}")


def show(ctx, f):
	for name in list(LISTS.values()) + archives(f.lists):
		info = f.lists.get(name)
		count = frappe.db.count("Item Price", {"price_list": name})
		say(
			ctx,
			f"  {name}: "
			+ (f"company {info.company or '-'}, enabled {info.enabled}, {count} prices" if info else "MISSING"),
		)
	for company, value in f.defaults.items():
		say(ctx, f"  {company}: default selling list {value or '-'}")
	say(ctx, f"  Quick Price page Custom Role {f.page_role or '-'}: {', '.join(f.page_roles) or '-'}")


# -- modes ----------------------------------------------------------------------------------------------------------------------


def plan(ctx):
	f = facts()
	show(ctx, f)
	references(ctx)
	problems = gates(ctx)
	for p in problems:
		say(ctx, f"  GATE {p}")
	say(ctx, "  would set: " + "; ".join(f"{c} default -> {n}" for c, n in LISTS.items()))
	say(
		ctx,
		"  would disable: "
		+ ", ".join(f"{n} (company {'-> ' + ARCHIVES[n] if ARCHIVES[n] else 'stays blank'})" for n in archives(f.lists)),
	)
	say(ctx, f"  would set the page roles: {', '.join(r for r in PAGE_ROLES if frappe.db.exists('Role', r))}")
	say(ctx, "PLAN: " + ("ready for apply" if not problems else f"{len(problems)} gate(s) fail: apply would stop"))


def apply(ctx):
	from fours_customizations import price_lists
	from fours_customizations.quick_price import api

	problems = gates(ctx)
	if problems:
		for p in problems:
			say(ctx, f"  GATE {p}")
		raise Stop("gates fail: nothing changed")
	before = load_before(ctx)
	f = facts()
	if not before:
		done = [c for c, n in LISTS.items() if f.defaults.get(c) == n] + [
			n for n in (SS, SPVN) if f.lists.get(n) and not f.lists[n].enabled
		]
		if done:
			raise Stop(
				f"already applied ({', '.join(done)}) but this stamp has no before-image: use the stamp that applied "
				"them (its ps2-before.json is the rollback), or decide by hand"
			)

	# 2 Company default lists
	if "defaults" not in before:
		before["defaults"] = f.defaults
		save_before(ctx, before)
	for company, name in LISTS.items():
		now = f.defaults.get(company)
		if now == name:
			continue
		if now not in (None, "", before["defaults"].get(company)):
			raise Stop(f"{company}: default list is {now}, neither the before-image nor {name}: drift, stop")
		frappe.db.set_value("Company", company, DEFAULT_FIELD, name)
		say(ctx, f"  {company}: default selling list -> {name}")
	frappe.db.commit()
	price_lists.clear_caches()

	# 3 archives, through the release's guard (an archive missing from an older before-image is recorded first)
	recorded = before.setdefault("archives", {})
	missing = [n for n in archives(f.lists) if n not in recorded]
	if missing:
		for n in missing:
			recorded[n] = {
				"enabled": f.lists[n].enabled,
				"company": f.lists[n].company,
				"prices": frappe.db.count("Item Price", {"price_list": n}),
			}
		save_before(ctx, before)
	for name in archives(f.lists):
		company = ARCHIVES[name]
		info = price_lists.lists()[name]
		if not info.enabled and info.company == company:
			continue
		was = before["archives"][name]
		if (info.enabled, info.company) != (was["enabled"], was["company"]) and info.enabled:
			raise Stop(f"{name}: enabled {info.enabled} company {info.company}, not the before-image: drift, stop")
		doc = frappe.get_doc("Price List", name)
		doc._doc_before_save = frappe.get_doc("Price List", name)
		doc.enabled = 0
		doc.set(FIELD, company)
		price_lists.validate_price_list(doc)  # the release's guard: privileged user, not in use
		frappe.db.set_value("Price List", name, {"enabled": 0, FIELD: company})
		frappe.cache.hdel("price_list_details", name)
		say(ctx, f"  {name}: disabled, company {company or '-'} ({frappe.db.count('Item Price', {'price_list': name})} prices kept)")
	frappe.db.commit()
	price_lists.clear_caches()

	# 4 page roles
	role = f.page_role
	if not role:
		raise Stop(f"no Custom Role for page {PAGE}")
	if "page_roles" not in before:
		before["page_roles"] = {"custom_role": role, "roles": f.page_roles}
		save_before(ctx, before)
	wanted = sorted(r for r in PAGE_ROLES if frappe.db.exists("Role", r))
	if sorted(f.page_roles) != wanted:
		doc = frappe.get_doc("Custom Role", role)
		doc.set("roles", [{"role": r} for r in wanted])
		doc.save(ignore_permissions=True)
		say(ctx, f"  page roles -> {', '.join(wanted)}")
	derived = sorted(r for r in api.page_roles() if frappe.db.exists("Role", r))
	if derived != wanted:
		say(ctx, f"  NOTE: Item Price read roles give {', '.join(derived)} (the next migrate's sync_access sets those)")
	frappe.db.commit()

	# 5 caches
	price_lists.clear_caches()
	frappe.clear_cache()
	say(ctx, "  caches cleared (fours_quick_price:*, price_list_details, site cache)")
	say(ctx, "APPLY done: run PS_MODE=verify")


def verify(ctx):
	from fours_customizations import price_lists
	from fours_customizations.quick_price import api

	f = facts()
	show(ctx, f)
	problems = gates(ctx)
	for company, name in LISTS.items():
		if f.defaults.get(company) != name or price_lists.company_default_list(company) != name:
			problems.append(f"{company}: default list {f.defaults.get(company)}, not {name}")
	for name in archives(f.lists):
		company = ARCHIVES[name]
		info = f.lists.get(name)
		if not info or info.enabled or info.company != company:
			problems.append(f"{name}: not a disabled archive of company {company or '-'}")
	wanted = sorted(r for r in PAGE_ROLES if frappe.db.exists("Role", r))
	if f.page_roles != wanted:
		problems.append(f"page roles {f.page_roles} != {wanted}")
	before = load_before(ctx)
	say(ctx, f"  before-image: {', '.join(sorted(before)) or 'none'}")

	# the Item Price rule per enabled price reader with a Company row
	rows = frappe.get_all("Item Price", fields=["name", "price_list", "item_code"], limit_page_length=0)
	read_roles = set(api.price_read_roles())
	for user in sorted(frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, pluck="name")):
		if user == "Administrator" or not (set(frappe.get_roles(user)) & read_roles):
			continue
		allowed = price_lists.allowed_companies(user)
		if allowed is None:
			say(ctx, f"  user {user_hash(user)}: unrestricted")
			continue
		expected = {r.name for r in rows if price_lists.row_visible(user, r.price_list, r.item_code)}
		got = set(frappe.get_list("Item Price", user=user, limit_page_length=0, pluck="name"))
		archived = {r.name for r in rows if r.name in got and r.price_list in ARCHIVES}
		other = {
			r.name
			for r in rows
			if r.name in got and f.lists.get(r.price_list) and f.lists[r.price_list].company not in (None, *allowed)
		}
		say(
			ctx,
			f"  user {user_hash(user)} ({', '.join(sorted(allowed))}): sees {len(got)}; rule allows {len(expected)} "
			f"(hidden by other User Permissions {len(expected - got)}); archive rows {len(archived)}; other-company rows "
			f"{len(other)}",
		)
		if got - expected or archived or other:
			problems.append(f"user {user_hash(user)}: Item Price visibility off the rule")
	for company in LISTS:
		lists = api.company_lists(company, api.list_usage())
		say(ctx, f"  Quick Price page lists of {company}: {', '.join(lists) or '-'}")
		if not lists or lists[0] != LISTS[company] or set(lists) & set(ARCHIVES):
			problems.append(f"page lists of {company}: {lists}")
	vox = frappe.db.count("Custom DocPerm", {"role": "Vox Accountant"})
	say(ctx, f"  Vox Accountant Custom DocPerm rows {vox}")
	if vox != VOX_ROWS:
		problems.append(f"Vox Accountant Custom DocPerm rows {vox}, not {VOX_ROWS}")
	for p in problems:
		say(ctx, f"  PROBLEM {p}")
	say(ctx, "VERIFY: " + ("OK" if not problems else f"{len(problems)} problem(s)"))


def rollback(ctx):
	"""Runs on the release and on fe153b5 (no import of the release's code)."""
	before = load_before(ctx)
	if not before:
		say(ctx, "  no ps2-before.json: apply never wrote anything under this stamp")
		return
	f = facts()
	if "page_roles" in before:
		role = before["page_roles"]["custom_role"]
		if sorted(f.page_roles) != sorted(before["page_roles"]["roles"]):
			doc = frappe.get_doc("Custom Role", role)
			doc.set("roles", [{"role": r} for r in before["page_roles"]["roles"]])
			doc.save(ignore_permissions=True)
			say(ctx, f"  page roles -> {', '.join(before['page_roles']['roles']) or '-'}")
		frappe.db.commit()
	for name, was in (before.get("archives") or {}).items():
		info = read_lists().get(name)
		if not info:
			say(ctx, f"  {name}: gone, nothing to restore")
			continue
		if (info.enabled, info.company) == (was["enabled"], was["company"]):
			continue
		if (info.enabled, info.company) != (0, ARCHIVES[name]):
			raise Stop(f"{name}: enabled {info.enabled} company {info.company} changed since apply: decide by hand")
		frappe.db.set_value("Price List", name, {"enabled": was["enabled"], FIELD: was["company"]})
		frappe.cache.hdel("price_list_details", name)
		say(ctx, f"  {name}: enabled {was['enabled']}, company {was['company'] or '-'}")
	frappe.db.commit()
	clear_caches()
	for company, was in (before.get("defaults") or {}).items():
		now = frappe.db.get_value("Company", company, DEFAULT_FIELD)
		if now == was:
			continue
		if now != LISTS.get(company):
			raise Stop(f"{company}: default list {now} changed since apply: decide by hand")
		frappe.db.set_value("Company", company, DEFAULT_FIELD, was)
		say(ctx, f"  {company}: default selling list -> {was or '-'}")
	frappe.db.commit()
	clear_caches()
	frappe.clear_cache()
	say(ctx, "ROLLBACK done (caches cleared): run PS_MODE=plan to see the state")


try:
	ps2_main()
except Stop as e:
	frappe.db.rollback()
	print(f"STOP: {e}", flush=True)

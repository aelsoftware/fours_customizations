"""fqp_check.py - checks for deploy_fqp.sh (ps-fours-2 price list split code release of fours_customizations on top of the
fqp-1 Quick Price release, multax.kit.africa).

Run by deploy_fqp.sh as the bench user from <bench>/sites with the bench's python:
    ../env/bin/python fqp_check.py <command> [options]
Every command is READ-ONLY except the two that say so ('vox-fix --apply', 'rollback-cleanup --apply'), which change only
--site. Output: ids, names, counts and hashes; never secrets (site_config values are hashed, never printed).
Exit codes: 0 ok, 2 gate failed, 3 nothing to do (pending), 1 error.

Commands
  sites      --out F                         every site: installed apps (from its database), maintenance/scheduler
  pending    --site S [--allow-app A,...] [--root app=path] [--no-gate] --out F
                                             patches not yet run and files 'bench migrate' would import, per app
  facts      --site S --out F                Vox Accountant Custom DocPerm rows, every Custom DocPerm row (hash), Item
                                             Price rules, site_config/kit_pos switches (hashes), release objects
  fingerprint --exclude S --out F            other sites, read-only fingerprint;  fp-compare --before F --after F
  vox-fix    --site S --baseline F [--expect N] [--apply]
                                             Vox Accountant Custom DocPerm rows back to the baseline: rows migrate
                                             re-added are removed, changed rows restored; count must equal --expect
  perms-compare --before F --after F         every Custom DocPerm row of every other role unchanged
  switches-compare --before F --after F      site_config and the kit_pos switches unchanged
  smoke      --site S [--since T]            hooks loaded, Company default-list field, split rules hold per restricted
                                             user (SQL = Python rule), consumers, page lists (or field absent with --pre)
  rollback-cleanup --site S [--apply]        the release's Company custom field, removed only while every value is blank
  ps2-state  --site S                        gate of the rollback: stops while ps2.py is applied (Company default lists
                                             set, Standard Selling / Selling Price Vox Nansana disabled)
  page-roles --site S --baseline F [--apply] the Quick Price page's Custom Role roles back to those in F (facts)
  jobs       --site S                        background jobs running for the site (RQ started registries)
  assets     --out F;  assets-compare --before F --after F   other apps' built assets unchanged by the build
  syntax     --dir D;  jsoncheck --dir D
"""

import argparse
import hashlib
import json
import os
import sys

APP = "fours_customizations"
PAGE = "quick-price-update"
LOG_DOCTYPE = "Quick Price Change"
WORKSPACE = "Pricing"
COMPANY_FIELD = "Company-custom_default_selling_price_list"  # the only record this release adds (after_migrate)
PL = "fours_customizations.price_lists."
SELLING_JS = "public/js/selling_price_list.js"
HOOKED = (  # (hook, doctype or None, event or None, method / file): every registration of the release in hooks.py
	("has_permission", "Item Price", None, PL + "item_price_has_permission"),
	("permission_query_conditions", "Item Price", None, PL + "item_price_query_conditions"),
	("doc_events", "Item Price", "validate", PL + "validate_item_price"),
	("doc_events", "Item", "validate", PL + "fill_item_default_price_lists"),
	("doc_events", "Price List", "validate", PL + "validate_price_list"),
	("doc_events", "Price List", "before_rename", PL + "before_rename_price_list"),
	("doc_events", "Price List", "on_update", PL + "forget"),
	("doc_events", "Price List", "after_rename", PL + "forget"),
	("doc_events", "Price List", "on_trash", PL + "forget"),
	("doc_events", "Company", "on_update", PL + "forget"),
	("doc_events", "Kit POS Location", "validate", PL + "validate_consumer_list"),
	("doc_events", "POS Profile", "validate", PL + "validate_consumer_list"),
	*(
		("doc_events", doctype, "validate", PL + "validate_party_default_list")
		for doctype in ("Customer", "Customer Group", "Supplier", "Supplier Group")
	),
	*(
		("doc_events", doctype, "before_validate", PL + "replace_disabled_selling_list")
		for doctype in ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "POS Invoice")
	),
	("before_request", None, None, PL + "patch_erpnext"),
	("before_job", None, None, PL + "patch_erpnext"),
	*(
		("doctype_js", doctype, None, SELLING_JS)
		for doctype in ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "POS Invoice")
	),
)
ARCHIVES = ("Standard Selling", "Selling Price Vox Nansana")  # disabled by ps2.py apply, enabled again by its rollback
VOX = "Vox Accountant"
RIGHTS = (
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"import",
	"share",
	"print",
	"email",
	"select",
	"set_user_permissions",
	"mask",
)


class GateFail(Exception):
	pass


def out(msg=""):
	print(msg, flush=True)


def sha(value):
	return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def dump(path, data):
	fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(fd, "w") as f:
		json.dump(data, f, indent=1, sort_keys=True, default=str)


def load(path):
	with open(path) as f:
		return json.load(f)


def all_sites():
	return sorted(d for d in os.listdir(".") if os.path.isfile(os.path.join(d, "site_config.json")))


def connect(site):
	import frappe

	frappe.init(site, sites_path=".")
	frappe.connect()
	return frappe


def close():
	import frappe

	try:
		frappe.db.rollback()
	finally:
		frappe.destroy()


def site_config(site):
	with open(os.path.join(site, "site_config.json")) as f:
		return json.load(f)


# -- sites ---------------------------------------------------------------------------------------------------------------


def cmd_sites(a):
	result = {}
	for site in all_sites():
		cfg = site_config(site)
		row = {
			"maintenance_mode": cfg.get("maintenance_mode", 0),
			"pause_scheduler": cfg.get("pause_scheduler", 0),
		}
		try:
			frappe = connect(site)
			row["installed_apps"] = frappe.get_installed_apps()
			row["enable_scheduler"] = frappe.db.get_single_value("System Settings", "enable_scheduler")
		except Exception as e:
			row["error"] = type(e).__name__
		finally:
			close()
		result[site] = row
		has = APP in row.get("installed_apps", [])
		out(
			f"  {site}: {APP} {'INSTALLED' if has else 'not installed'}; {len(row.get('installed_apps', []))} apps; "
			f"maintenance {row['maintenance_mode']}; {'ERROR ' + row['error'] if 'error' in row else ''}"
		)
	dump(a.out, result)
	if any("error" in r for r in result.values()):
		raise GateFail("a site could not be read")


# -- pending -------------------------------------------------------------------------------------------------------------


def would_import(frappe, path):
	"""import_file_by_path's decision without importing (frappe/modules/import_file.py)."""
	from frappe.modules.import_file import calculate_hash, read_doc_from_file
	from frappe.utils import get_datetime

	try:
		docs = read_doc_from_file(path)
	except Exception:
		return []
	if not docs:
		return []
	docs = docs if isinstance(docs, list) else [docs]
	found = []
	for doc in docs:
		if not isinstance(doc, dict) or not doc.get("doctype") or not doc.get("name"):
			continue
		try:
			db_modified = frappe.db.get_value(doc["doctype"], doc["name"], "modified")
		except Exception:
			db_modified = None
		if not db_modified:
			found.append((doc["doctype"], doc["name"], "new"))
			continue
		if doc["doctype"] == "DocType":
			stored = frappe.db.get_value("DocType", doc["name"], "migration_hash")
			if stored and stored == calculate_hash(path):
				continue
		elif get_datetime(doc.get("modified")) <= get_datetime(db_modified):
			continue
		found.append((doc["doctype"], doc["name"], "changed"))
	return found


def app_files(frappe, app, root=None):
	from frappe.model.sync import get_doc_files
	from frappe.modules.utils import get_app_level_directory_path

	files = []
	app_path = root or frappe.get_app_path(app)
	for module in frappe.local.app_modules.get(app) or []:
		folder = os.path.join(app_path, frappe.scrub(module))
		if os.path.isdir(folder):
			files = get_doc_files(files=files, start_path=folder)
	for name in ("desktop_icon", "workspace_sidebar", "sidebar_item_group"):
		folder = os.path.join(app_path, name) if root else get_app_level_directory_path(name, app)
		if os.path.isdir(folder):
			files += [os.path.join(folder, f) for f in sorted(os.listdir(folder))]
	return files


def cmd_pending(a):
	frappe = connect(a.site)
	try:
		from frappe.modules.patch_handler import get_patches_from_app

		roots = dict(r.split("=", 1) for r in (a.root or []))
		if roots:
			for app, path in roots.items():
				# a release tree's modules.txt may name new modules
				mod_file = os.path.join(path, "modules.txt")
				if os.path.exists(mod_file):
					mods = [m.strip() for m in open(mod_file) if m.strip() and not m.startswith("#")]
					frappe.local.app_modules[app] = [frappe.scrub(m) for m in mods]
		executed = set(frappe.get_all("Patch Log", pluck="patch"))
		allowed = set(filter(None, (a.allow_app or "").split(",")))
		result, blocking = {}, []
		for app in frappe.get_installed_apps():
			try:
				listed = _patches_in(roots[app]) if app in roots else get_patches_from_app(app)
				patches = [p for p in listed if p not in executed]
			except Exception as e:
				patches = [f"(unreadable patches.txt: {type(e).__name__})"]
			files = []
			for path in app_files(frappe, app, roots.get(app)):
				files += would_import(frappe, path)
			result[app] = {"patches": patches, "imports": files}
			if patches or files:
				out(f"  {app}: {len(patches)} pending patch(es), {len(files)} file(s) migrate would import")
				for p in patches[:10]:
					out(f"    patch {p}")
				for dt, name, how in files[:25]:
					out(f"    {how:7} {dt} {name}")
				if app not in allowed:
					blocking.append(app)
		dump(a.out, result)
		if not any(r["patches"] or r["imports"] for r in result.values()):
			out("  nothing pending for any app of the site")
		if blocking and not a.no_gate:
			raise GateFail(
				f"migrate would also run/import for: {', '.join(blocking)} (not this release): read the list; "
				"FQP_ACK_SYNC=<apps> accepts it"
			)
	finally:
		close()


def _patches_in(app_path):
	from frappe.modules.patch_handler import parse_as_configfile

	path = os.path.join(app_path, "patches.txt")
	if not os.path.exists(path):
		return []
	try:
		return parse_as_configfile(path)
	except Exception:
		return [line.strip() for line in open(path) if line.strip() and not line.startswith(("#", "["))]


# -- facts ---------------------------------------------------------------------------------------------------------------


def docperm_rows(frappe, role=None):
	filters = {"role": role} if role else {}
	fields = ["name", "parent", "role", "permlevel", "if_owner", *RIGHTS]
	have = set(frappe.db.get_table_columns("Custom DocPerm"))
	rows = frappe.get_all(
		"Custom DocPerm", filters=filters, fields=[f for f in fields if f in have], order_by="name"
	)
	return {r.name: {k: v for k, v in r.items() if k != "name"} for r in rows}


def switches(site, frappe=None):
	# pause_scheduler is the kit's own switch (code pauses, reload resumes): not part of the comparison
	cfg = {k: v for k, v in site_config(site).items() if k != "pause_scheduler"}
	data = {
		"site_config_sha": sha(cfg),
		"site_config_keys": sorted(cfg),
		"kit_pos_keys": {k: sha(v) for k, v in cfg.items() if k.startswith(("kit_pos", "vox_"))},
	}
	if frappe:
		rows = frappe.db.sql(
			"select doctype, field, value from tabSingles where doctype like 'Kit POS%%' order by 1, 2"
		)
		data["kit_pos_singles"] = {f"{d}.{f}": sha(v) for d, f, v in rows}
	return data


def cmd_facts(a):
	frappe = connect(a.site)
	try:
		vox = docperm_rows(frappe, VOX)
		allrows = docperm_rows(frappe)
		item_price = [r for r in allrows.values() if r["parent"] == "Item Price"] or [
			dict(r)
			for r in frappe.get_all(
				"DocPerm",
				filters={"parent": "Item Price"},
				fields=["role", "permlevel", "if_owner", "read", "write", "create"],
			)
		]
		facts = {
			"site": a.site,
			"db_name": frappe.conf.db_name,
			"app_head": _git_head(frappe.get_app_path(APP)),
			"vox_accountant_rows": vox,
			"vox_accountant_count": len(vox),
			"custom_docperm": allrows,
			"custom_docperm_count": len(allrows),
			"item_price_rules": sorted(item_price, key=lambda r: (r["role"], r["permlevel"])),
			"switches": switches(a.site, frappe),
			"release": release_objects(frappe),
			"patch_log": frappe.db.count("Patch Log"),
		}
		try:
			facts["db_size_mb"] = int(
				frappe.db.sql(
					"select sum(data_length + index_length) / 1048576 from "
					"information_schema.tables where table_schema = database()"
				)[0][0]
				or 0
			)
		except Exception:
			facts["db_size_mb"] = 0
		dump(a.out, facts)
		out(
			f"  {a.site}: Vox Accountant Custom DocPerm rows {len(vox)}; Custom DocPerm rows {len(allrows)}; "
			f"site_config sha {facts['switches']['site_config_sha']}; kit_pos keys {len(facts['switches']['kit_pos_keys'])}; "
			f"Kit POS singles {len(facts['switches']['kit_pos_singles'])}; app HEAD {facts['app_head'][:12]}"
		)
		w = sorted({r["role"] for r in item_price if r.get("write") or r.get("create")})
		rd = sorted(
			{
				r["role"]
				for r in item_price
				if r.get("read") and not r.get("permlevel") and not r.get("if_owner")
			}
		)
		out(f"  Item Price read: {', '.join(rd)}")
		out(f"  Item Price write/create: {', '.join(w)}")
		rel = facts["release"]
		out(
			f"  fqp-1 objects: DocType {rel['doctype']}, Page {rel['page']}, Workspace {rel['workspace']}, "
			f"Custom Role roles {rel['custom_role_roles']}, {LOG_DOCTYPE} rows {rel['log_rows']}"
		)
		out(
			f"  ps-fours-2: Company field {rel['company_field']}; Phase 1 Price List.custom_company field "
			f"{rel['price_list_company_field']}"
		)
		if a.expect_vox is not None and len(vox) != a.expect_vox:
			raise GateFail(
				f"Vox Accountant Custom DocPerm rows are {len(vox)}, not the expected {a.expect_vox}: the "
				"baseline changed since 2026-10-22 - the lead decides before any migrate"
			)
		if a.expect == "pre" and rel["company_field"]:
			raise GateFail(f"Custom Field {COMPANY_FIELD} already exists: not the pre-release baseline")
		if a.expect == "pre" and not (rel["doctype"] and rel["page"]):
			raise GateFail("the fqp-1 Quick Price objects are missing: the baseline fe153b5 is not what the site runs")
	finally:
		close()


def _git_head(app_path):
	head = os.path.join(os.path.dirname(app_path), ".git", "HEAD")
	try:
		ref = open(head).read().strip()
		if ref.startswith("ref: "):
			p = os.path.join(os.path.dirname(app_path), ".git", ref[5:])
			return open(p).read().strip() if os.path.exists(p) else ref
		return ref
	except OSError:
		return "?"


def release_objects(frappe):
	exists = frappe.db.exists
	role = frappe.db.get_value("Custom Role", {"page": PAGE}, "name") if exists("Page", PAGE) else None
	return {
		"doctype": bool(exists("DocType", LOG_DOCTYPE)),
		"page": bool(exists("Page", PAGE)),
		"workspace": bool(exists("Workspace", WORKSPACE)),
		"custom_role": role,
		"custom_role_roles": sorted(
			frappe.get_all("Has Role", filters={"parenttype": "Custom Role", "parent": role}, pluck="role")
		)
		if role
		else [],
		"log_rows": frappe.db.sql(f"select count(*) from `tab{LOG_DOCTYPE}`")[0][0]
		if frappe.db.table_exists(LOG_DOCTYPE)
		else None,  # raw: the DocType may be gone (rollback)
		"company_field": bool(exists("Custom Field", COMPANY_FIELD)),
		"price_list_company_field": bool(exists("Custom Field", "Price List-custom_company")),
	}


# -- fingerprints of the other sites ---------------------------------------------------------------------------------------


def cmd_fingerprint(a):
	result = {"sites": {}}
	for site in all_sites():
		if site == a.exclude:
			continue
		cfg = site_config(site)
		fp = {"site_config_sha": sha(cfg), "maintenance_mode": cfg.get("maintenance_mode", 0)}
		try:
			frappe = connect(site)
			fp["patch_log"] = frappe.db.count("Patch Log")
			fp["doctypes"] = frappe.db.count("DocType")
			fp["custom_docperm"] = frappe.db.count("Custom DocPerm")
			fp["custom_docperm_sha"] = sha(docperm_rows(frappe))
			fp["vox_accountant_custom_docperm"] = frappe.db.count("Custom DocPerm", {"role": VOX})
			fp["enable_scheduler"] = frappe.db.get_single_value("System Settings", "enable_scheduler")
			fp["installed_apps"] = frappe.get_installed_apps()
			fp["max_doctype_modified"] = str(frappe.db.sql("select max(modified) from tabDocType")[0][0])
			r = frappe.db.sql(
				"select count(distinct table_name), count(*) from information_schema.columns "
				"where table_schema = database()"
			)[0]
			fp["schema_tables_columns"] = [int(r[0]), int(r[1])]
		except Exception as e:
			fp["error"] = type(e).__name__
		finally:
			close()
		result["sites"][site] = fp
		out(
			f"  {site}: patches {fp.get('patch_log')}, doctypes {fp.get('doctypes')}, Custom DocPerm "
			f"{fp.get('custom_docperm')} (Vox Accountant {fp.get('vox_accountant_custom_docperm')}), tables/columns "
			f"{fp.get('schema_tables_columns')}, maintenance {fp['maintenance_mode']}, scheduler {fp.get('enable_scheduler')}"
			f"{', ERROR ' + fp['error'] if 'error' in fp else ''}"
		)
	dump(a.out, result)
	if any("error" in fp for fp in result["sites"].values()):
		raise GateFail("a site could not be fingerprinted")


def cmd_fp_compare(a):
	before, after = load(a.before)["sites"], load(a.after)["sites"]
	changed = []
	for site in sorted(set(before) | set(after)):
		b, c = before.get(site), after.get(site)
		if b != c:
			changed.append(site)
			keys = sorted(k for k in set(b or {}) | set(c or {}) if (b or {}).get(k) != (c or {}).get(k))
			out(f"  CHANGED {site}: {', '.join(keys)}")
		else:
			out(f"  unchanged {site}")
	if changed:
		raise GateFail(
			f"other sites changed: {', '.join(changed)} - find out what changed them (this kit never runs a "
			"command on another site)"
		)


# -- Vox Accountant rows ------------------------------------------------------------------------------------------------------


def _label(row):
	rights = ",".join(k for k in RIGHTS if row.get(k))
	return f"{row['parent']} permlevel {row['permlevel']} if_owner {row['if_owner']} [{rights}]"


def cmd_vox_fix(a):
	base = load(a.baseline)["vox_accountant_rows"]
	frappe = connect(a.site)
	try:
		now = docperm_rows(frappe, VOX)
		added = sorted(set(now) - set(base))
		missing = sorted(set(base) - set(now))
		changed = sorted(n for n in set(now) & set(base) if now[n] != base[n])
		out(
			f"  Vox Accountant Custom DocPerm rows: baseline {len(base)}, now {len(now)}; re-added {len(added)}, "
			f"changed {len(changed)}, missing {len(missing)}"
		)
		for n in added:
			out(f"    re-added: {_label(now[n])}")
		for n in changed:
			out(f"    changed:  {_label(now[n])}  (baseline {_label(base[n])})")
		for n in missing:
			out(f"    missing:  {_label(base[n])}")
		if missing:
			raise GateFail(
				"baseline Vox Accountant rows are missing after migrate: restore them from the baseline file "
				"by hand (the lead decides); nothing changed by this check"
			)
		if (added or changed) and a.apply:
			doctypes = set()
			for n in added:
				doctypes.add(now[n]["parent"])
				frappe.db.delete("Custom DocPerm", {"name": n, "role": VOX})
			for n in changed:
				doctypes.add(base[n]["parent"])
				values = {k: v for k, v in base[n].items() if k not in ("parent", "role")}
				frappe.db.set_value("Custom DocPerm", n, values, update_modified=False)
			frappe.db.commit()
			for dt in doctypes:
				frappe.clear_cache(doctype=dt)
			now = docperm_rows(frappe, VOX)
			out(f"  applied: removed {len(added)}, restored {len(changed)}; rows now {len(now)}")
		elif added or changed:
			raise GateFail("Vox Accountant rows differ from the baseline (dry run: re-run with --apply)")
		if now != base:
			raise GateFail("Vox Accountant rows still differ from the baseline")
		if a.expect is not None and len(now) != a.expect:
			raise GateFail(f"Vox Accountant Custom DocPerm rows {len(now)}, expected {a.expect}")
		out(f"  Vox Accountant Custom DocPerm rows = baseline ({len(now)})")
	finally:
		close()


def cmd_perms_compare(a):
	b = {n: r for n, r in load(a.before)["custom_docperm"].items() if r["role"] != VOX}
	c = {n: r for n, r in load(a.after)["custom_docperm"].items() if r["role"] != VOX}
	diff = sorted(n for n in set(b) | set(c) if b.get(n) != c.get(n))
	for n in diff:
		r = c.get(n) or b.get(n)
		how = "new" if n not in b else "removed" if n not in c else "changed"
		out(f"    {how}: {r['role']} on {_label(r)}")
	out(f"  Custom DocPerm rows of other roles: {len(b)} before, {len(c)} after, {len(diff)} different")
	if diff:
		raise GateFail(
			"permissions of other roles changed during migrate (this release changes none): the lead decides "
			"(FQP_ACK_PERMS=1 after reading the list)"
		)


def cmd_switches_compare(a):
	b, c = load(a.before)["switches"], load(a.after)["switches"]
	diff = sorted(k for k in set(b) | set(c) if b.get(k) != c.get(k))
	for k in diff:
		if isinstance(b.get(k), dict) and isinstance(c.get(k), dict):
			keys = sorted(x for x in set(b[k]) | set(c[k]) if b[k].get(x) != c[k].get(x))
			out(f"    {k}: {', '.join(keys)}")
		else:
			out(f"    {k} changed")
	if diff:
		raise GateFail("site_config or the kit_pos switches changed")
	out(
		f"  site_config (sha {c['site_config_sha']}), {len(c['kit_pos_keys'])} kit_pos/vox keys and "
		f"{len(c.get('kit_pos_singles', {}))} Kit POS settings values unchanged"
	)


# -- smoke -----------------------------------------------------------------------------------------------------------------


def site_time(frappe, utc):
	"""A UTC stamp of the kit (2026-10-23T12:00:00Z) as the site's local time, as Error Log.creation is stored."""
	from datetime import datetime, timezone
	from zoneinfo import ZoneInfo

	from frappe.utils import get_system_timezone

	moment = datetime.strptime(utc.rstrip("Z"), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
	return moment.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)


def cmd_smoke(a):
	frappe = connect(a.site)
	try:
		rel = release_objects(frappe)
		problems = []
		if a.pre:
			loaded = [m for hook, doctype, event, m in HOOKED if m in hook_values(frappe, hook, doctype, event)]
			if loaded:
				problems.append(f"price_lists hooks still loaded after rollback: {len(loaded)}")
			out(f"  price_lists hooks loaded: {len(loaded)}; Custom Field {COMPANY_FIELD}: {rel['company_field']}")
		else:
			problems += _smoke_release(frappe, rel)
			errors = (
				frappe.get_all("Error Log", filters={"creation": [">=", site_time(frappe, a.since)]}, fields=["method", "error"])
				if a.since
				else []
			)
			mine = [
				e
				for e in errors
				if any(k in f"{e.method} {e.error}".lower() for k in ("price_lists", "quick price", "quick_price"))
			]
			out(f"  Error Log since {a.since or '-'}: {len(errors)} rows, {len(mine)} mention price_lists / Quick Price")
			if mine:
				problems.append(f"{len(mine)} Error Log rows of this release")
		if problems:
			for p in problems:
				out(f"    PROBLEM {p}")
			raise GateFail("smoke failed")
		out("  smoke OK")
	finally:
		close()


def hook_values(frappe, hook, doctype, event):
	"""The values registered for (hook, doctype, event) in the loaded hooks (a list)."""
	got = frappe.get_hooks(hook) or ([] if doctype is None else {})
	if doctype is not None:
		got = got.get(doctype, {}) if isinstance(got, dict) else {}
		if event:
			got = got.get(event, []) if isinstance(got, dict) else []
	return got if isinstance(got, list) else [got]


def _user_hash(user):
	return hashlib.sha1(user.encode()).hexdigest()[:8]


def _smoke_release(frappe, rel):
	"""Read-only: the hooks load, the Company field is there, and with the split active the Item Price rule holds for
	every enabled restricted user who reads prices (the permission query gives no row the Python rule refuses - other
	User Permissions, e.g. Price List rows, may hide more - and none of another company's list or of a disabled list),
	consumers use lists they may, and the page lists per company."""
	from fours_customizations import price_lists
	from fours_customizations.quick_price import api

	problems = []
	for hook, doctype, event, method in HOOKED:
		if method not in hook_values(frappe, hook, doctype, event):
			problems.append(f"hook {hook} {doctype or ''} {event or ''} lacks {method}")
	out(f"  hooks: {len(HOOKED) - len(problems)}/{len(HOOKED)} price_lists hooks loaded")
	from erpnext.stock import get_item_details
	from erpnext.stock.doctype.item.item import Item

	price_lists.patch_erpnext()
	wrapped = [
		getattr(f, "fours_price_lists", False) for f in (get_item_details.insert_item_price, Item.add_price)
	]
	out(f"  ERPNext automatic price writes wrapped: {sum(wrapped)}/2")
	if not all(wrapped):
		problems.append("ERPNext insert_item_price / Item.add_price not wrapped (signature changed?)")
	if not rel["company_field"]:
		problems.append(f"Custom Field {COMPANY_FIELD} missing (after_migrate did not run?)")
	elif not frappe.db.get_value("Custom Field", COMPANY_FIELD, "ignore_user_permissions"):
		problems.append(f"Custom Field {COMPANY_FIELD} must ignore user permissions")
	price_lists.clear_caches()
	active = price_lists.split_active()
	lists = price_lists.lists()
	out(f"  split active: {active}")
	counts = dict(frappe.db.sql("select price_list, count(*) from `tabItem Price` group by price_list"))
	for name, info in sorted(lists.items()):
		default_of = [c for c in frappe.get_all("Company", pluck="name") if price_lists.company_default_list(c) == name]
		out(
			f"    {name}: company {info.company or '-'}, enabled {info.enabled}, selling {info.selling}, buying "
			f"{info.buying}, {info.currency}, {counts.get(name, 0)} prices"
			+ (f", default of {', '.join(default_of)}" if default_of else "")
		)
	if not active:
		out("  the split is not active (no Price List has a company): every rule is off, behaviour = fe153b5")
		return problems

	# the rule per restricted user: SQL (permission query) = Python (has_permission) on every Item Price row
	rows = frappe.get_all("Item Price", fields=["name", "price_list", "item_code"], limit_page_length=0)
	read_roles = set(api.price_read_roles())
	users = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, pluck="name")
	checked = 0
	for user in sorted(users):
		if user == "Administrator" or not (set(frappe.get_roles(user)) & read_roles):
			continue
		allowed = price_lists.allowed_companies(user)
		if allowed is None:
			out(f"    user {_user_hash(user)}: unrestricted (no Company row for prices)")
			continue
		expected = {r.name for r in rows if price_lists.row_visible(user, r.price_list, r.item_code)}
		got = set(
			frappe.get_list("Item Price", fields=["name"], user=user, limit_page_length=0, pluck="name")
		)
		foreign = {
			r.name
			for r in rows
			if r.name in got
			and (
				not lists.get(r.price_list)
				or not lists[r.price_list].enabled
				or (lists[r.price_list].company and lists[r.price_list].company not in allowed)
			)
		}
		checked += 1
		out(
			f"    user {_user_hash(user)} ({', '.join(sorted(allowed))}): sees {len(got)} of {len(rows)} prices; "
			f"rule allows {len(expected)} (hidden by other User Permissions {len(expected - got)}); "
			f"other-company/disabled {len(foreign)}"
		)
		if got - expected or foreign:
			problems.append(f"user {_user_hash(user)}: permission query differs from the rule or leaks")
	out(f"  restricted price readers checked: {checked}")

	# item families of the company lists (the guard refuses new ones; existing ones are reported)
	for name, info in sorted(lists.items()):
		if info.company and info.company in price_lists.families():
			off = [
				r.item_code
				for r in rows
				if r.price_list == name and not price_lists.family_allows(r.item_code, info.company)
			]
			out(f"    {name}: {len(off)} prices outside {info.company}'s item family" + (f" (e.g. {off[0]})" if off else ""))

	# consumers: active Kit POS Locations / enabled POS Profiles on an enabled list of their company or of none
	consumers = frappe.get_all("POS Profile", filters={"disabled": 0}, fields=["name", "company", "selling_price_list"])
	if frappe.db.table_exists("Kit POS Location"):
		consumers += frappe.get_all(
			"Kit POS Location", filters={"active": 1}, fields=["name", "company", "selling_price_list"]
		)
	bad = [
		c.name
		for c in consumers
		if c.selling_price_list
		and (
			not lists.get(c.selling_price_list)
			or not lists[c.selling_price_list].enabled
			or (lists[c.selling_price_list].company or c.company) != c.company
		)
	]
	out(f"  consumers (enabled POS Profiles + active Kit POS Locations): {len(consumers)}, off-rule {len(bad)}")
	if bad:
		problems.append(f"consumers on a disabled list or another company's list: {', '.join(bad)}")

	# the Quick Price page per company (Administrator)
	frappe.set_user("Administrator")
	usage = api.list_usage()
	for company in frappe.get_all("Company", pluck="name", order_by="name"):
		out(f"    page lists of {company}: {', '.join(api.company_lists(company, usage)) or '-'}")
	return problems


def cmd_rollback_cleanup(a):
	"""The code is back at fe153b5: remove the release's Company custom field, but only while no company has a value (the
	data step's own rollback, ps2.py, clears them first; otherwise the values would be lost)."""
	frappe = connect(a.site)
	try:
		exists = frappe.db.exists("Custom Field", COMPANY_FIELD)
		values = (
			frappe.get_all(
				"Company",
				filters={"custom_default_selling_price_list": ["is", "set"]},
				fields=["name", "custom_default_selling_price_list"],
			)
			if exists
			else []
		)
		out(f"  left after rollback: Custom Field {COMPANY_FIELD} {bool(exists)}; companies with a value {len(values)}")
		if values:
			for v in values:
				out(f"    {v.name}: {v.custom_default_selling_price_list}")
			out(
				"  WARN: Company default lists are set (ps2.py was not rolled back before the code): the field stays."
				" Run ps2.py with PS_MODE=rollback and the Phase 2 stamp (it runs on fe153b5 too), then"
				" 'fqp_check.py rollback-cleanup --site <site> --apply' to remove the field (fe153b5 ignores it)."
			)
			return
		if a.apply and exists:
			frappe.delete_doc("Custom Field", COMPANY_FIELD, ignore_permissions=True, force=True)
			frappe.db.commit()
			frappe.clear_cache(doctype="Company")
			out(f"  removed Custom Field {COMPANY_FIELD}")
	finally:
		close()


def cmd_ps2_state(a):
	"""Read-only gate of the kit's rollback: the code may go back to fe153b5 only after 'ps2.py rollback' (fe153b5 has no
	replace hooks: with the archives disabled, amendments / mapped documents of pre-split documents would break)."""
	frappe = connect(a.site)
	try:
		problems = []
		if frappe.db.has_column("Company", "custom_default_selling_price_list"):
			n = frappe.db.count("Company", {"custom_default_selling_price_list": ["is", "set"]})
			if n:
				problems.append(f"{n} Company default selling list(s) set")
		for name in ARCHIVES:
			if frappe.db.get_value("Price List", name, "enabled") == 0:
				problems.append(f"{name} disabled")
		out(f"  ps2 data step: {'; '.join(problems) if problems else 'not applied (or rolled back)'}")
		if problems:
			raise GateFail("ps2.py is applied: run 'ps2.py' with PS_MODE=rollback (same PS_TS) first, then the kit's rollback")
	finally:
		close()


def cmd_page_roles(a):
	"""fours' after_migrate (quick_price.setup.sync_access) rewrites the page's Custom Role on every migrate; the deploy keeps
	the roles the page had before it (Phase 1 paused the page: System Manager only), ps2.py resumes it."""
	base = load(a.baseline)["release"]
	frappe = connect(a.site)
	try:
		role = frappe.db.get_value("Custom Role", {"page": PAGE}, "name")
		now = sorted(frappe.get_all("Has Role", filters={"parenttype": "Custom Role", "parent": role}, pluck="role")) if role else []
		want = sorted(base.get("custom_role_roles") or [])
		out(f"  page roles before the deploy: {', '.join(want) or '-'}; now: {', '.join(now) or '-'}")
		if not role or not want or now == want:
			return
		if a.apply:
			doc = frappe.get_doc("Custom Role", role)
			doc.set("roles", [{"role": r} for r in want])
			doc.save(ignore_permissions=True)
			frappe.db.commit()
			frappe.clear_cache()
			out(f"  page roles -> {', '.join(want)}")
		else:
			raise GateFail("page roles differ from the baseline")
	finally:
		close()


# -- jobs, assets, syntax ------------------------------------------------------------------------------------------------------


def cmd_jobs(a):
	connect(a.site)
	try:
		from frappe.utils.background_jobs import get_queue_list, get_redis_conn
		from rq import Queue
		from rq.registry import StartedJobRegistry

		conn = get_redis_conn()
		running = []
		for q in get_queue_list():
			queue = Queue(q, connection=conn)
			for job_id in StartedJobRegistry(queue=queue).get_job_ids():
				job = queue.fetch_job(job_id)
				site = job and (job.kwargs or {}).get("site")
				if not a.site_only or site == a.site:
					running.append(
						(
							q,
							site,
							(job.kwargs or {}).get("job_name") or (job.kwargs or {}).get("method")
							if job
							else job_id,
						)
					)
		out(f"  running background jobs{' of ' + a.site if a.site_only else ''}: {len(running)}")
		for q, site, name in running[:10]:
			out(f"    {q}: {site} {str(name)[:80]}")
		if running and a.fail_any:
			raise GateFail("background jobs are running")
	except GateFail:
		raise
	except Exception as e:
		out(
			f"  WARN: job check not possible ({type(e).__name__}): check 'bench --site {a.site} show-pending-jobs' by hand"
		)
	finally:
		close()


def cmd_assets(a):
	result = {}
	assets = "assets"
	if os.path.exists(os.path.join(assets, "assets.json")):
		manifest = load(os.path.join(assets, "assets.json"))
		for key, value in manifest.items():
			app = value.split("/")[2] if value.startswith("/assets/") else "?"
			result.setdefault(app, {})[key] = value
	for entry in sorted(os.listdir(assets)):
		p = os.path.join(assets, entry)
		result.setdefault("_links", {})[entry] = (
			os.readlink(p) if os.path.islink(p) else ("dir" if os.path.isdir(p) else "file")
		)
	dump(a.out, result)
	out(
		f"  assets: {sum(len(v) for k, v in result.items() if k != '_links')} bundle entries, {len(result.get('_links', {}))} entries"
	)


def cmd_assets_compare(a):
	b, c = load(a.before), load(a.after)
	diff = [k for k in sorted(set(b) | set(c)) if k not in (APP,) and b.get(k) != c.get(k)]
	if "_links" in diff:
		ld = [k for k in set(b["_links"]) | set(c["_links"]) if b["_links"].get(k) != c["_links"].get(k)]
		if set(ld) <= {APP}:
			diff.remove("_links")
		else:
			out(f"    asset links changed: {', '.join(sorted(ld))}")
	if diff:
		raise GateFail(f"the build changed other apps' assets: {', '.join(diff)}")
	out("  other apps' assets unchanged")


def cmd_syntax(a):
	import ast

	bad = 0
	n = 0
	for root, _dirs, files in os.walk(a.dir):
		for f in files:
			if f.endswith(".py"):
				n += 1
				try:
					ast.parse(open(os.path.join(root, f)).read())
				except SyntaxError as e:
					bad += 1
					out(f"    syntax error {os.path.join(root, f)}: {e}")
	out(f"  python files {n}, syntax errors {bad}")
	if bad:
		raise GateFail("syntax errors")


def cmd_jsoncheck(a):
	bad = 0
	n = 0
	for root, _dirs, files in os.walk(a.dir):
		for f in files:
			if f.endswith(".json"):
				n += 1
				try:
					json.load(open(os.path.join(root, f)))
				except ValueError as e:
					bad += 1
					out(f"    bad json {os.path.join(root, f)}: {e}")
	out(f"  json files {n}, bad {bad}")
	if bad:
		raise GateFail("bad json")


def main():
	p = argparse.ArgumentParser()
	sub = p.add_subparsers(dest="cmd", required=True)
	s = sub.add_parser("sites")
	s.add_argument("--out", required=True)
	s = sub.add_parser("pending")
	s.add_argument("--site", required=True)
	s.add_argument("--out", required=True)
	s.add_argument("--allow-app")
	s.add_argument("--root", action="append")
	s.add_argument("--no-gate", action="store_true")
	s = sub.add_parser("facts")
	s.add_argument("--site", required=True)
	s.add_argument("--out", required=True)
	s.add_argument("--expect-vox", type=int)
	s.add_argument("--expect", choices=["pre"])
	s = sub.add_parser("fingerprint")
	s.add_argument("--exclude", required=True)
	s.add_argument("--out", required=True)
	s = sub.add_parser("fp-compare")
	s.add_argument("--before", required=True)
	s.add_argument("--after", required=True)
	s = sub.add_parser("vox-fix")
	s.add_argument("--site", required=True)
	s.add_argument("--baseline", required=True)
	s.add_argument("--expect", type=int)
	s.add_argument("--apply", action="store_true")
	s = sub.add_parser("perms-compare")
	s.add_argument("--before", required=True)
	s.add_argument("--after", required=True)
	s = sub.add_parser("switches-compare")
	s.add_argument("--before", required=True)
	s.add_argument("--after", required=True)
	s = sub.add_parser("smoke")
	s.add_argument("--site", required=True)
	s.add_argument("--pre", action="store_true")
	s.add_argument("--since")
	s = sub.add_parser("rollback-cleanup")
	s.add_argument("--site", required=True)
	s.add_argument("--apply", action="store_true")
	s = sub.add_parser("ps2-state")
	s.add_argument("--site", required=True)
	s = sub.add_parser("page-roles")
	s.add_argument("--site", required=True)
	s.add_argument("--baseline", required=True)
	s.add_argument("--apply", action="store_true")
	s = sub.add_parser("jobs")
	s.add_argument("--site", required=True)
	s.add_argument("--site-only", action="store_true")
	s.add_argument("--fail-any", action="store_true")
	s = sub.add_parser("assets")
	s.add_argument("--out", required=True)
	s = sub.add_parser("assets-compare")
	s.add_argument("--before", required=True)
	s.add_argument("--after", required=True)
	s = sub.add_parser("syntax")
	s.add_argument("--dir", required=True)
	s = sub.add_parser("jsoncheck")
	s.add_argument("--dir", required=True)
	a = p.parse_args()
	try:
		globals()["cmd_" + a.cmd.replace("-", "_")](a)
	except GateFail as e:
		out(f"GATE FAILED: {e}")
		sys.exit(2)


if __name__ == "__main__":
	main()

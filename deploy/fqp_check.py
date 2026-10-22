"""fqp_check.py - checks for deploy_fqp.sh (fqp-1 Quick Price Update in fours_customizations, multax.kit.africa).

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
  smoke      --site S [--release]            release objects present and consistent (or absent with --pre)
  rollback-cleanup --site S [--apply]        the release's Custom Role / Pricing workspace records left after rollback
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
			f"  release objects: DocType {rel['doctype']}, Page {rel['page']}, Workspace {rel['workspace']}, "
			f"Custom Role roles {rel['custom_role_roles']}, {LOG_DOCTYPE} rows {rel['log_rows']}"
		)
		if a.expect_vox is not None and len(vox) != a.expect_vox:
			raise GateFail(
				f"Vox Accountant Custom DocPerm rows are {len(vox)}, not the expected {a.expect_vox}: the "
				"baseline changed since 2026-10-22 - the lead decides before any migrate"
			)
		if a.expect == "pre" and (rel["doctype"] or rel["page"]):
			raise GateFail("release objects already exist on the site: not the pre-release baseline")
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


def cmd_smoke(a):
	frappe = connect(a.site)
	try:
		from frappe.utils import get_datetime

		rel = release_objects(frappe)
		problems = []
		if a.pre:
			if rel["doctype"] or rel["page"] or rel["workspace"] or rel["custom_role"]:
				problems.append(f"release objects present after rollback: {rel}")
		else:
			from fours_customizations.quick_price import api

			for key in ("doctype", "page", "workspace"):
				if not rel[key]:
					problems.append(f"{key} missing")
			expected = sorted(r for r in api.page_roles() if frappe.db.exists("Role", r))
			if rel["custom_role_roles"] != expected:
				problems.append(
					f"Custom Role roles {rel['custom_role_roles']} != Item Price read roles {expected}"
				)
			for hook, value in (("ignore_links_on_delete", LOG_DOCTYPE),):
				if value not in frappe.get_hooks(hook):
					problems.append(f"hook {hook} lacks {value}")
			if "Item Price" not in (frappe.get_hooks("doctype_list_js") or {}):
				problems.append("Item Price list script not hooked")
			frappe.set_user("Administrator")
			ctx = api.get_context()
			out(
				f"  get_context (Administrator): {len(ctx['companies'])} companies; {len(ctx['selling_lists'])} "
				"enabled selling lists"
			)
			for company in ctx["companies"]:
				data = api.get_items(company, page_length=5)
				lists = ", ".join(f"{l['price_list']} (used by {len(l['used_by'])})" for l in data["lists"])
				out(f"    {company}: lists {lists}; first page {len(data['items'])} items")
			log_rows = rel["log_rows"]
			out(f"  {LOG_DOCTYPE} rows {log_rows}; page roles {', '.join(expected)}")
			writers = sorted(
				{
					r.role
					for r in frappe.get_all(
						"Custom DocPerm",
						filters={"parent": "Item Price", "permlevel": 0},
						fields=["role", "write", "create"],
					)
					if r.write or r.create
				}
			)
			out(f"  Item Price write/create roles (unchanged by this release): {', '.join(writers) or '-'}")
			errors = (
				frappe.get_all(
					"Error Log", filters={"creation": [">=", get_datetime(a.since)]}, pluck="method"
				)
				if a.since
				else []
			)
			mine = [e for e in errors if "quick price" in (e or "").lower() or APP in (e or "")]
			out(
				f"  Error Log since {a.since or '-'}: {len(errors)} rows, {len(mine)} mention Quick Price / {APP}"
			)
			if mine:
				problems.append(f"{len(mine)} Error Log rows of this release")
		if problems:
			for p in problems:
				out(f"    PROBLEM {p}")
			raise GateFail("smoke failed")
		out("  smoke OK")
	finally:
		close()


def cmd_rollback_cleanup(a):
	frappe = connect(a.site)
	try:
		rel = release_objects(frappe)
		role = frappe.db.get_value("Custom Role", {"page": PAGE}, "name")
		left = {
			"custom_role": role,
			"workspace": frappe.db.exists("Workspace", WORKSPACE),
			"workspace_sidebar": frappe.db.exists("Workspace Sidebar", WORKSPACE)
			if frappe.db.table_exists("Workspace Sidebar")
			else None,
			"desktop_icon": frappe.db.exists("Desktop Icon", WORKSPACE)
			if frappe.db.table_exists("Desktop Icon")
			else None,
			"page": rel["page"],
			"doctype": rel["doctype"],
			"log_rows": rel["log_rows"],
		}
		out(f"  left after rollback: {left}")
		if a.apply:
			if role:
				frappe.delete_doc("Custom Role", role, ignore_permissions=True, force=True)
			for dt in ("Desktop Icon", "Workspace Sidebar", "Workspace"):
				if (
					frappe.db.table_exists(dt)
					and frappe.db.exists(dt, WORKSPACE)
					and frappe.db.get_value(dt, WORKSPACE, "app" if dt != "Workspace" else "module")
					in (APP, "Fours Customizations")
				):
					frappe.delete_doc(dt, WORKSPACE, ignore_permissions=True, force=True)
			frappe.db.commit()
			frappe.clear_cache()
			out(
				"  removed the release's Custom Role / Pricing records (the Quick Price Change table and rows stay)"
			)
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

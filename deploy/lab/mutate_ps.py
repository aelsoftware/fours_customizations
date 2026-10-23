"""ps-fours-2 LAB ONLY: hand mutants of the price list split code; each must make a test module fail. Run as the bench user
in the private lab bench:  cd <bench> && env/bin/python <this file> <site> [label ...]   (files restored after every
mutant). Modules: test_price_lists, then test_quick_price_update for mutants of the page API."""

import subprocess
import sys

APP = "apps/fours_customizations/fours_customizations/"
PL = APP + "price_lists.py"
API = APP + "quick_price/api.py"
LCV = APP + "landed_cost_handler.py"
HOOKS = APP + "hooks.py"
QPC = APP + "fours_customizations/doctype/quick_price_change/quick_price_change.py"
MUTANTS = [
	("write: stored row not checked", PL, 'not in (*READ_PTYPES, "create") and doc.get("name"):', "not in (*READ_PTYPES, \"create\") and False:"),
	("disabled lists visible", PL, "	if not info.enabled:\n		return False\n	if info.company:", "	if info.company:"),
	("shared lists: no family rule", PL, "	owners = item_companies(item_code)\n	if owners:", "	return True\n	owners = item_companies(item_code)\n	if owners:"),
	("items of no family visible to all", PL, "	return bool(allowed - set(families()))\n\n\ndef item_price_has", "	return True\n\n\ndef item_price_has"),
	("company rows of any applicable_for", PL, 'if (row.get("applicable_for") or "") in APPLICABLE_FOR}', "}"),
	("device session for any user", PL, "	if user == frappe.session.user and getattr(frappe.local, \"kit_pos_device\", None):", "	if getattr(frappe.local, \"kit_pos_device\", None):"),
	("query: shared lists without family", PL, "		if groups:\n			conditions.append(", "		if groups:\n			conditions.append(f\"`tabItem Price`.`price_list` in ({_sql_in(shared)})\")\n		if False:\n			conditions.append("),
	("query: disabled own lists", PL, "own = sorted(n for n, i in lists().items() if i.enabled and i.company", "own = sorted(n for n, i in lists().items() if i.company"),
	("family guard off", PL, "	if not info or not info.company or family_allows(doc.item_code, info.company):\n		return", "	return"),
	("family allowlist always true", PL, "	return bool(item_roots(item_code) & set(roots))", "	return True"),
	("price list: any user changes guarded fields", PL, "	if changed and not is_privileged():", "	if False:"),
	("price list: restricted System Manager privileged", PL, '"System Manager" in frappe.get_roles(user) and allowed_companies(user) is None', '"System Manager" in frappe.get_roles(user)'),
	("price list: new selling list without company", PL, '		if cint(doc.selling) and not doc.get(FIELD):\n			frappe.throw', "		if False:\n			frappe.throw"),
	("price list: disable while used", PL, "	if cint(before.enabled) and not cint(doc.enabled):\n		check_not_in_use(doc.name)", "	pass"),
	("in use: Kit POS Location ignored", PL, '				"Kit POS Location", filters={"selling_price_list": price_list, "active": 1}, pluck="name"', '				"Kit POS Location", filters={"name": "-"}, pluck="name"'),
	("rename allowed", PL, "	if split_active() and not is_privileged():\n		frappe.throw(_(\"Only an administrator may rename", "	if False:\n		frappe.throw(_(\"Only an administrator may rename"),
	("consumer: other company allowed", PL, "	if info.company and info.company != doc.get(\"company\"):", "	if False:"),
	("consumer: disabled list allowed", PL, "	if not info or not info.enabled:\n		frappe.throw(_(\"Price list {0} is disabled", "	if False:\n		frappe.throw(_(\"Price list {0} is disabled"),
	("party: other company allowed", PL, "	if not info or (info.company not in allowed and not (shared_ok and not info.company)):", "	if not info:"),
	("documents keep a disabled list", PL, "	if info and info.enabled:\n		return price_list\n	default", "	return price_list\n	default"),
	("documents: currency ignored", PL, "	if currency and lists()[default].currency != currency:", "	if False:"),
	("before_validate not hooked", HOOKS, '			"fours_customizations.price_lists.replace_disabled_selling_list",\n			"fours_customizations.sales_invoice_handler.before_validate",', '			"fours_customizations.sales_invoice_handler.before_validate",'),
	("item defaults not filled", PL, "		if row.company and not row.default_price_list:", "		if False:"),
	("item defaults: existing items filled", PL, "	if not doc.is_new() or not split_active():", "	if not split_active():"),
	("landed cost: all lists", LCV, '		filters["price_list"] = ["in", scope or [""]]', "		pass"),
	("landed cost: other currency kept in scope", LCV, "info.company == doc.company and info.currency == currency", "info.company == doc.company"),
	("page: Selling Settings fallback kept", API, "	if price_lists.split_active():\n		default = price_lists.company_default_list(company)", "	if False:\n		default = price_lists.company_default_list(company)"),
	("page: owned lists shared", API, "	if owner:\n		return [owner]", "	if False:\n		return [owner]"),
	("page: other companies' lists selectable", API, "		and (companies is None or price_lists.list_info(row.name).company in companies | {None})", ""),
	("page: hidden cells shown", API, "	return not price_lists.split_active() or price_lists.row_visible(", "	return True or price_lists.row_visible("),
	("page: family lock dropped", API, "	return bool(info and info.company and not price_lists.family_allows(item_code, info.company))", "	return False"),
	("page: any Company row", API, "		allowed = price_lists.allowed_companies(user)  # the desk's rule for prices", '		allowed = user_permission_values(user, "Company", applies_to=None)'),
	("auto-insert into archives", PL, "	if not info.enabled:\n		return True\n	if info.company and cint(", "	if False:\n		return True\n	if info.company and cint("),
	("auto-insert into other families", PL, "	if info.company and cint(frappe.conf.get(GUARD_CONFIG, 1)) and not family_allows(item_code, info.company):\n		return True", "	if False:\n		return True"),
	("auto-insert: restricted writer's hidden row inserted", PL, "	if not row_visible(user, price_list, item_code):\n		noted", "	if False:\n		noted"),
	("auto-insert: judged for the session user, not the given one", PL, "	user = user or frappe.session.user\n	if not row_visible(", "	user = frappe.session.user\n	if not row_visible("),
	("ERPNext: signature not checked", PL, "	if not signatures_match(", "	if False and not signatures_match("),
	("ERPNext: signature warning every call", PL, "		if not _signature_warned:", "		if True:"),
	("ERPNext: only one function checked", PL, '			"Item.add_price": tuple(inspect.signature(add_price).parameters),\n', ""),
	("price list: title unguarded", PL, '("price_list_name", "enabled",', '("enabled",'),
	("price list: restricted user creates another company's list", PL, "	if allowed is not None and doc.get(FIELD) and doc.get(FIELD) not in allowed:", "	if False:"),
	("log: no company rule in the query", QPC, '	if scope is None:\n		return ""', '	if True:\n		return ""'),
	("log: no company rule in has_permission", QPC, "	if scope is None:\n		return True", "	if True:\n		return True"),
	("log: other companies' lists readable", QPC, "	return doc.get(\"company\") in allowed and doc.get(\"price_list\") not in foreign", "	return doc.get(\"company\") in allowed"),
	("ERPNext not patched", PL, "\n\npatch_erpnext()\n", "\n"),
	("party: shared selling list for customers", PL, '	shared_ok = doc.doctype in ("Supplier", "Supplier Group")', "	shared_ok = True"),
	("party: disabled default allowed", PL, "	if info and not info.enabled:\n		frappe.throw(_(\"Price list {0} is disabled: it cannot be a default.", "	if False:\n		frappe.throw(_(\"Price list {0} is disabled: it cannot be a default."),
	("guard: true vs 1 is a change", PL, "	if fieldname in CHECK_FIELDS:\n		return cint(value)", "	if False:\n		return cint(value)"),
	("landed cost: other family created", LCV, "		if price_lists.automatic_price_refused(default_list, item_code):\n			return 0", "		if False:\n			return 0"),
	("dark: split active without companies", PL, "		memo[\"active\"] = has_company_field() and any(info.company for info in lists().values())", "		memo[\"active\"] = has_company_field()"),
]


def run_tests(site, module):
	r = subprocess.run(
		["bench", "--site", site, "run-tests", "--module", f"fours_customizations.tests.{module}"],
		capture_output=True,
		text=True,
	)
	out = r.stdout + r.stderr
	summary = [l for l in out.splitlines() if l.startswith(("Ran ", "OK", "FAILED"))]
	return r.returncode, " ".join(summary)


def run_all(site, path):
	rc, summary = run_tests(site, "test_price_lists")
	if rc == 0 and path == API:
		rc, more = run_tests(site, "test_quick_price_update")
		summary += " | qp: " + more
	return rc, summary


site = sys.argv[1]
only = sys.argv[2:]
for module in ("test_price_lists", "test_quick_price_update"):
	rc, summary = run_tests(site, module)
	print(f"baseline {module}: rc={rc} {summary}", flush=True)
	if rc != 0 or " OK" not in f" {summary}" or "Ran 0 " in summary:
		sys.exit("baseline must run and pass (allow_tests on the site?)")
killed = total = 0
for label, path, old, new in MUTANTS:
	if only and label not in only:
		continue
	src = open(path).read()
	if src.count(old) != 1:
		print(f"SKIP (pattern x{src.count(old)}): {label}", flush=True)
		continue
	total += 1
	try:
		open(path, "w").write(src.replace(old, new))
		rc, summary = run_all(site, path)
	finally:
		open(path, "w").write(src)
	killed += rc != 0
	print(f"{'killed  ' if rc else 'SURVIVED'} {label}: {summary}", flush=True)
print(f"killed {killed}/{total} (of {len(MUTANTS)} listed)")

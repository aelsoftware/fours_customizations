"""fqp-1 LAB ONLY: hand mutants of the Quick Price Update code; each must make the test module fail. Run as the bench user in
the private lab bench:  cd <bench> && env/bin/python <this file> <site>   (files are restored after every mutant)."""

import subprocess
import sys

APP = "apps/fours_customizations/fours_customizations/"
API = APP + "quick_price/api.py"
SETUP = APP + "quick_price/setup.py"
LOG = APP + "fours_customizations/doctype/quick_price_change/quick_price_change.py"
MUTANTS = [
	(
		"stale check dropped",
		API,
		'	if flt(change.get("expected_old_rate") or 0, precision) != flt(old_rate or 0, precision):\n		raise_changed',
		"	if False:\n		raise_changed",
	),
	(
		"effective-record check dropped",
		API,
		'		if current.get("item_price") != doc.name:\n			raise_changed',
		"		if False:\n			raise_changed",
	),
	(
		"created-meanwhile check dropped",
		API,
		'		if current.get("item_price"):\n			raise_changed',
		"		if False:\n			raise_changed",
	),
	("large change: >= instead of >", API, "* 100, 6) > LARGE_CHANGE_PCT", "* 100, 6) >= LARGE_CHANGE_PCT"),
	(
		"large change: confirm ignored",
		API,
		"if is_large_change(old_rate, new_rate) and not confirm_large:",
		"if is_large_change(old_rate, new_rate) and False:",
	),
	(
		"large change: only increases",
		API,
		"flt(abs(flt(new_rate) - flt(old_rate))",
		"flt((flt(new_rate) - flt(old_rate))",
	),
	("100-change cap dropped", API, "	if len(changes) > MAX_CHANGES:", "	if False:"),
	(
		"company access dropped",
		API,
		"	if not company or company not in accessible_companies(user):",
		"	if not company:",
	),
	(
		"Company User Permission ignored",
		API,
		'	allowed = user_permission_values(user, "Company", applies_to=None)',
		"	allowed = None",
	),
	(
		"Price List User Permission ignored",
		API,
		'	allowed = user_permission_values(user, "Price List")',
		"	allowed = None",
	),
	("Item Price read check dropped", API, "	if not can_read_prices():", "	if False:"),
	(
		"cell write dry run dropped",
		API,
		'		if not frappe.has_permission("Item Price", "write", doc=doc):\n			return {"editable": 0',
		'		if False:\n			return {"editable": 0',
	),
	(
		"cell create dry run dropped",
		API,
		"	if not can_create:\n		return",
		"	if False:\n		return",
	),
	(
		"duplicate cell editable",
		API,
		'	if cell.get("duplicate"):\n		return',
		"	if False:\n		return",
	),
	(
		"duplicate save check dropped",
		API,
		"	if (item.name, change.price_list) in duplicate_cells([item.name], [change.price_list]):",
		"	if False:",
	),
	(
		"UOM factor ignored",
		API,
		'\t\t\t\t"rate": flt(row.price_list_rate) * flt(factor or 1),',
		'\t\t\t\t"rate": flt(row.price_list_rate),',
	),
	(
		"UOM factor ignored on a blank-unit record",
		API,
		'\n\t\t\t"rate": flt(row.price_list_rate) * flt(factor or 1),',
		'\n\t\t\t"rate": flt(row.price_list_rate),',
	),
	(
		"unit check dropped",
		API,
		"	if uom != item.stock_uom and not frappe.db.exists(",
		"	if False and not frappe.db.exists(",
	),
	("Kit TV Viewer not excluded", API, 'EXCLUDED_ROLES = ("Kit TV Viewer",)', "EXCLUDED_ROLES = ()"),
	(
		"System Manager not added",
		API,
		'	return sorted({r for r in price_read_roles() if r not in EXCLUDED_ROLES} | {"System Manager"})',
		"	return sorted({r for r in price_read_roles() if r not in EXCLUDED_ROLES})",
	),
	(
		"if_owner read counted",
		API,
		"if cint(r.read) and not cint(r.permlevel) and not cint(r.if_owner)",
		"if cint(r.read) and not cint(r.permlevel)",
	),
	("log row not written", API, "	log.insert()\n", "	pass\n"),
	(
		"log old rate wrong",
		API,
		'"old_rate": old_rate if status == "changed" else 0,',
		'"old_rate": new_rate,',
	),
	(
		"shared notice dropped",
		API,
		"	others = [c for c in users if c != company]\n	if not others:",
		"	others = []\n	if not others:",
	),
	(
		"also-sold-by includes own company",
		API,
		"			if company == ctx.company:\n				continue",
		"			if False:\n				continue",
	),
	(
		"Item Defaults not counted",
		API,
		"where parenttype = 'Item' and company = %s",
		"where parenttype = 'Item' and company = %s and 1=0",
	),
	(
		"Bins not counted",
		API,
		"join `tabWarehouse` wh on wh.name = bin.warehouse where wh.company = %s",
		"join `tabWarehouse` wh on wh.name = bin.warehouse where wh.company = %s and 1=0",
	),
	(
		"old invoices counted",
		API,
		'inv.posting_date >= %s"""',
		'inv.posting_date >= date_sub(%s, interval 1000 day)"""',
	),
	(
		"old list usage counted",
		API,
		"where docstatus = 1 and posting_date >= %s and ifnull",
		"where docstatus = 1 and posting_date >= date_sub(%s, interval 1000 day) and ifnull",
	),
	("fresh check dropped", API, "	assert_fresh(item.name, change.price_list)\n", "\n"),
	(
		"fresh check compares nothing",
		API,
		"	if committed != sorted(frappe.db.sql(query, (item_code, price_list))):",
		"	if False:",
	),
	(
		"locking order dropped",
		API,
		"key=lambda i: (str(changes[i].item_code), str(changes[i].price_list))",
		"key=lambda i: i",
	),
	(
		"deadlock treated as one refusal",
		API,
		"				aborted = isinstance(e, frappe.QueryDeadlockError)",
		"				aborted = False",
	),
	(
		"Item User Permission ignored",
		API,
		'		allowed_items=user_permission_values(user, "Item"),',
		"		allowed_items=None,",
	),
	(
		"blank-unit record read as the sales-unit price",
		API,
		"		if row.uom == uom or (not row.uom and uom == stock_uom):",
		"		if row.uom == uom or not row.uom:",
	),
	("only-unpriced filter dropped", API, "		query = query.where(missing)", "		pass"),
	("bad rates accepted", API, "	if not rate > 0 or rate > MAX_RATE:", "	if rate > MAX_RATE:"),
	("list not on page accepted", API, "	if change.price_list not in ctx.lists:", "	if False:"),
	(
		"savepoint rollback dropped",
		API,
		'			frappe.db.rollback(save_point="fours_quick_price")',
		"			pass",
	),
	(
		"sync does not remove",
		SETUP,
		'		doc.set("roles", [{"role": role} for role in roles])',
		'		doc.set("roles", [{"role": role} for role in sorted(set(roles) | set(current))])',
	),
	(
		"log readable by all",
		LOG,
		"	return is_system_manager(user) or can_read_prices(user)",
		"	return True",
	),
	("log editable by all", LOG, "		elif not is_system_manager():", "		elif False:"),
	(
		"log insert by anyone",
		LOG,
		"			if not (self.flags.from_quick_price_update or frappe.flags.in_install or frappe.flags.in_patch):",
		"			if False:",
	),
]


def run_tests(site):
	r = subprocess.run(
		[
			"bench",
			"--site",
			site,
			"run-tests",
			"--module",
			"fours_customizations.tests.test_quick_price_update",
		],
		capture_output=True,
		text=True,
	)
	out = r.stdout + r.stderr
	summary = [l for l in out.splitlines() if l.startswith(("Ran ", "OK", "FAILED"))]
	return r.returncode, " ".join(summary)


site = sys.argv[1]
only = sys.argv[2:]
rc, summary = run_tests(site)
print(f"baseline: rc={rc} {summary}", flush=True)
if rc != 0:
	sys.exit("baseline must pass")
killed = 0
for label, path, old, new in MUTANTS:
	if only and label not in only:
		continue
	src = open(path).read()
	if src.count(old) != 1:
		print(f"SKIP (pattern x{src.count(old)}): {label}", flush=True)
		continue
	try:
		open(path, "w").write(src.replace(old, new))
		rc, summary = run_tests(site)
	finally:
		open(path, "w").write(src)
	killed += rc != 0
	print(f"{'killed  ' if rc else 'SURVIVED'} {label}: {summary}", flush=True)
print(f"killed {killed}/{len(MUTANTS)}")

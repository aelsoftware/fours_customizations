"""fqp-1 LAB ONLY: time the page API on the multax stand-in as a lab user (read-only: get_context / get_items).
cd <bench>/sites && ../env/bin/python <this file> fqpm.lab <user> [runs]"""

import sys
import time

import frappe

site, user = sys.argv[1], sys.argv[2]
runs = int(sys.argv[3]) if len(sys.argv) > 3 else 3
frappe.init(site, sites_path=".")
frappe.connect()
from fours_customizations.quick_price import api

frappe.set_user(user)
count = {"n": 0}
sql = frappe.db.sql


def counting(*args, **kwargs):
	count["n"] += 1
	return sql(*args, **kwargs)


frappe.db.sql = counting
ctx = api.get_context()
for company in ctx["companies"]:
	for run in range(runs):
		if run == 0:
			api.clear_cache()
		count["n"] = 0
		t0 = time.perf_counter()
		data = api.get_items(company, page_length=100)
		ms = (time.perf_counter() - t0) * 1000
		cells = sum(len(i["prices"]) for i in data["items"])
		print(
			f"{company}: run {run + 1}{' (cold cache)' if run == 0 else ''}: {len(data['items'])} items, {cells} cells, "
			f"{len(data['warehouses'])} warehouse columns, {count['n']} queries, {ms:.0f} ms"
		)
frappe.db.rollback()
frappe.destroy()

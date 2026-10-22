"""fqp-1 LAB ONLY: make the multax stand-in's Item Price rules look like production's (2026-10-22 facts) and add two lab
users for the browser check. Run as frappe in the private bench's sites dir:
    ../env/bin/python <this file> fqpm.lab <passwords file (written, 600)>
Production facts mirrored: Item Price read for 4S Accountant and Sales User; read/write/create for Sales Master Manager,
Purchase Master Manager and Vox Accountant (Kit TV Viewer read stays as the lab has it).
Users: fqp.writer@labvox.invalid (Vox Accountant; Company User Permissions Vox Lounge + Vox Lounge Nansana) and
fqp.reader@labvox.invalid (4S Accountant; Company User Permission 4S Industries Limited), fqp.tv@labvox.invalid (Kit TV
Viewer only: Item Price read without the page). Passwords are random, written
only to the given file; never printed."""

import json
import os
import secrets
import sys

import frappe
from frappe.permissions import add_permission, update_permission_property

site, out = sys.argv[1], sys.argv[2]
frappe.init(site, sites_path=".")
frappe.connect()
frappe.set_user("Administrator")

RULES = {
	"4S Accountant": ("read",),
	"Sales User": ("read",),
	"Vox Accountant": ("read", "write", "create"),
	"Sales Master Manager": ("read", "write", "create"),
	"Purchase Master Manager": ("read", "write", "create"),
}
for role, rights in RULES.items():
	if not frappe.db.exists(
		"Custom DocPerm", {"parent": "Item Price", "role": role, "permlevel": 0, "if_owner": 0}
	):
		add_permission("Item Price", role, 0)
	for right in rights:
		update_permission_property("Item Price", role, 0, right, 1, validate=False)
frappe.clear_cache(doctype="Item Price")

USERS = {
	"fqp.writer@labvox.invalid": ("Vox Accountant", ["Vox Lounge", "Vox Lounge Nansana"]),
	"fqp.reader@labvox.invalid": ("4S Accountant", ["4S Industries Limited"]),
	"fqp.tv@labvox.invalid": ("Kit TV Viewer", []),  # Item Price read, but no page
}
passwords = {}
for email, (role, companies) in USERS.items():
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"roles": [{"role": role}],
			}
		).insert(ignore_permissions=True)
	for company in companies:
		if not frappe.db.exists("User Permission", {"user": email, "allow": "Company", "for_value": company}):
			frappe.get_doc(
				{"doctype": "User Permission", "user": email, "allow": "Company", "for_value": company}
			).insert(ignore_permissions=True)
	passwords[email] = secrets.token_urlsafe(18)
	from frappe.utils.password import update_password

	update_password(email, passwords[email])

try:  # absent on the baseline code (the deploy rehearsal seeds before the release)
	from fours_customizations.quick_price.setup import sync_access
except ImportError:
	sync_access = None
if sync_access:
	sync_access()
frappe.db.commit()
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
	json.dump(passwords, f)
print("seeded", sorted(passwords))
frappe.destroy()

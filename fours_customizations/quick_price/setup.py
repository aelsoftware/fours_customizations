"""Quick Price Update access (fqp-1): who sees the page, the Pricing workspace and its desk icon.

The page's roles follow Item Price read on the site (fours_customizations.quick_price.api.page_roles: every role with
full Item Price read except Kit TV Viewer, plus System Manager). sync_access() writes that role set to
- the page's Custom Role record (what desk search, the sidebar and the page itself go by; with a Custom Role the page's
  own standard roles are not used for the menu, so System Manager is part of the set),
- the roles of the 'Pricing' Workspace and of its Desktop Icon (so the icon is not shown to users without the page).

It runs on every migrate (install.after_install) and, after the owner changes who may read Item Prices, by hand:
  bench --site multax.kit.africa execute fours_customizations.quick_price.setup.sync_access
It never changes any DocPerm / Custom DocPerm: who may update prices stays the site's Item Price permissions.
"""

import frappe

from fours_customizations.quick_price.api import PAGE, page_roles

WORKSPACE = "Pricing"
DESKTOP_ICON = "Pricing"


def sync_access(verbose=True):
	"""Idempotent. Returns {target: {"added": [...], "removed": [...]}} for what changed."""
	roles = [role for role in page_roles() if frappe.db.exists("Role", role)]
	changes = {}
	if frappe.db.exists("Page", PAGE):
		changes["Custom Role"] = sync_custom_role(roles)
	if frappe.db.exists("Workspace", WORKSPACE):
		changes["Workspace"] = sync_has_role("Workspace", WORKSPACE, roles)
	if frappe.db.exists("DocType", "Desktop Icon") and frappe.db.exists("Desktop Icon", DESKTOP_ICON):
		changes["Desktop Icon"] = sync_has_role("Desktop Icon", DESKTOP_ICON, roles)
	if any(change["added"] or change["removed"] for change in changes.values()):
		frappe.clear_cache()
	if verbose:
		for target, change in changes.items():
			if change["added"] or change["removed"]:
				print(f"Quick Price Update {target}: added {change['added']}, removed {change['removed']}")
		print(f"Quick Price Update roles: {', '.join(roles)}")
	return changes


def sync_custom_role(roles):
	name = frappe.db.get_value("Custom Role", {"page": PAGE}, "name")
	if name:
		doc = frappe.get_doc("Custom Role", name)
	else:
		doc = frappe.new_doc("Custom Role")
		doc.page = PAGE
	current = [row.role for row in doc.roles]
	change = {"added": sorted(set(roles) - set(current)), "removed": sorted(set(current) - set(roles))}
	if change["added"] or change["removed"] or doc.is_new():
		doc.set("roles", [{"role": role} for role in roles])
		doc.save(ignore_permissions=True)
	return change


def sync_has_role(parenttype, parent, roles):
	"""Set the Has Role rows of a standard record without saving it (a save would export it on a developer site)."""
	current = frappe.get_all(
		"Has Role",
		filters={"parenttype": parenttype, "parent": parent, "parentfield": "roles"},
		fields=["name", "role"],
	)
	have = {row.role for row in current}
	change = {"added": sorted(set(roles) - have), "removed": sorted(have - set(roles))}
	for row in current:
		if row.role in change["removed"]:
			frappe.db.delete("Has Role", {"name": row.name})
	for idx, role in enumerate(change["added"], len(current) + 1):
		frappe.get_doc(
			{
				"doctype": "Has Role",
				"parenttype": parenttype,
				"parent": parent,
				"parentfield": "roles",
				"role": role,
				"idx": idx,
			}
		).db_insert()
	return change

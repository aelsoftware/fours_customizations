"""
landed_cost_handler.py — Req #8.

When a Landed Cost Voucher is submitted, walk its items and — for each
`new_selling_price` that is set — update the standard *selling* Item Price
to that value.

The standard selling price list per company is taken from `Selling Settings`
(`selling_price_list`); we update *all* selling Item Prices for that item if
no price list is found there.

Price list split (price_lists.py): once lists belong to companies, only the
item's prices in enabled selling lists of the voucher's company, in the
company currency, change; a missing price goes to the company's default list.

The new selling price is per stock unit: an Item Price in another UOM (a carton) is set to it times that UOM's
conversion factor, and a created price carries the stock UOM.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, nowdate

from fours_customizations import price_lists


def on_submit(doc, method=None) -> None:
	if not doc.items:
		return

	scope = None
	if price_lists.split_active():
		currency = frappe.get_cached_value("Company", doc.company, "default_currency")
		scope = [
			name
			for name, info in price_lists.lists().items()
			if info.enabled and info.selling and info.company == doc.company and info.currency == currency
		]
		default_list = price_lists.company_default_list(doc.company)
		if default_list not in scope:
			default_list = None
	else:
		default_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")

	updated = 0
	for row in doc.items:
		new_price = flt(getattr(row, "new_selling_price", 0))
		if new_price <= 0 or not row.item_code:
			continue
		updated += _sync_item_price(row.item_code, new_price, default_list, scope)

	if updated:
		frappe.msgprint(
			f"Updated {updated} selling Item Price record(s) from Landed Cost Voucher {doc.name}.",
			alert=True,
		)


def _sync_item_price(
	item_code: str, new_price: float, default_list: str | None, scope: list[str] | None = None
) -> int:
	"""Update every selling Item Price for `item_code` (only in the `scope` lists when given). Creates one if none exist.

	`new_price` is a stock-UOM price: a row in another UOM (a carton) gets it times that UOM's conversion factor, the
	way ERPNext derives it, and a row in a UOM the item has no factor for is left alone."""
	filters = {"item_code": item_code, "selling": 1}
	if scope is not None:
		filters["price_list"] = ["in", scope or [""]]
	prices = frappe.get_all("Item Price", filters=filters, fields=["name", "price_list", "uom"])
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")

	if not prices and default_list:
		if price_lists.automatic_price_refused(default_list, item_code):
			return 0  # price list split: never into an archive or another item family's list
		ip = frappe.new_doc("Item Price")
		ip.item_code = item_code
		ip.price_list = default_list
		ip.selling = 1
		ip.uom = stock_uom
		ip.price_list_rate = new_price
		ip.valid_from = nowdate()
		ip.flags.ignore_permissions = True
		ip.insert(ignore_permissions=True)
		return 1

	factors = {
		row.uom: flt(row.conversion_factor)
		for row in frappe.get_all(
			"UOM Conversion Detail",
			filters={"parenttype": "Item", "parent": item_code},
			fields=["uom", "conversion_factor"],
		)
	}
	count = 0
	for p in prices:
		factor = 1.0 if not p.uom or p.uom == stock_uom else factors.get(p.uom)
		if not factor:
			continue
		frappe.db.set_value("Item Price", p.name, "price_list_rate", new_price * factor)
		count += 1
	return count

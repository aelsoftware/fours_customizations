"""
landed_cost_handler.py — Req #8.

When a Landed Cost Voucher is submitted, walk its items and — for each
`new_selling_price` that is set — update the standard *selling* Item Price
to that value.

The standard selling price list per company is taken from `Selling Settings`
(`selling_price_list`); we update *all* selling Item Prices for that item if
no price list is found there.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, nowdate


def on_submit(doc, method=None) -> None:
	if not doc.items:
		return

	default_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")

	updated = 0
	for row in doc.items:
		new_price = flt(getattr(row, "new_selling_price", 0))
		if new_price <= 0 or not row.item_code:
			continue
		updated += _sync_item_price(row.item_code, new_price, default_list)

	if updated:
		frappe.msgprint(
			f"Updated {updated} selling Item Price record(s) from Landed Cost Voucher {doc.name}.",
			alert=True,
		)


def _sync_item_price(item_code: str, new_price: float, default_list: str | None) -> int:
	"""Update every selling Item Price for `item_code`. Creates one if none exist."""
	filters = {"item_code": item_code, "selling": 1}
	prices = frappe.get_all("Item Price", filters=filters, fields=["name", "price_list"])

	if not prices and default_list:
		ip = frappe.new_doc("Item Price")
		ip.item_code = item_code
		ip.price_list = default_list
		ip.selling = 1
		ip.price_list_rate = new_price
		ip.valid_from = nowdate()
		ip.flags.ignore_permissions = True
		ip.insert(ignore_permissions=True)
		return 1

	count = 0
	for p in prices:
		frappe.db.set_value("Item Price", p.name, "price_list_rate", new_price)
		count += 1
	return count

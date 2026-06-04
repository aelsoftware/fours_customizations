"""
Patch: unlink cancelled Sales Orders from Sales Invoice / Delivery Note items.

A cancelled Sales Order left referenced on an invoice or delivery-note item makes
those documents fail to **amend** with "Cannot link cancelled document". Going
forward `sales_order_handler.on_cancel` clears the links when an order is
cancelled; this patch cleans up orders that were cancelled before that existed.

Idempotent — re-running only ever clears links that still point at a docstatus-2
Sales Order.
"""

import frappe


def execute() -> None:
	# Sales Invoice Item → cancelled Sales Order
	frappe.db.sql(
		"""
		UPDATE `tabSales Invoice Item` sii
		JOIN `tabSales Order` so ON so.name = sii.sales_order
		SET sii.sales_order = NULL, sii.so_detail = NULL
		WHERE so.docstatus = 2
		"""
	)

	# Delivery Note Item → cancelled Sales Order
	frappe.db.sql(
		"""
		UPDATE `tabDelivery Note Item` dni
		JOIN `tabSales Order` so ON so.name = dni.against_sales_order
		SET dni.against_sales_order = NULL, dni.so_detail = NULL
		WHERE so.docstatus = 2
		"""
	)

	frappe.db.commit()
	print("4S patch: unlinked cancelled Sales Orders from Sales Invoice / Delivery Note items")

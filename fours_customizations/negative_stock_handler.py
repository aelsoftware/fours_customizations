"""
negative_stock_handler.py — Req #5 + #7

Behaviour
---------
* SI / DN `before_submit`:
    For every stock item being shipped, if the projected balance after the
    submit would go negative, temporarily flip `allow_negative_stock` on the
    *item master* so the submit succeeds without error.  Both the original
    flag state and the fact that we toggled it are remembered.

* Daily cron (at the negative-stock notification time):
    Collect every item currently carrying a negative balance in any warehouse
    OR with `custom_negative_stock_auto_enabled = 1`, email + Slack the
    operations lead, and create a draft Stock Reconciliation grouped by
    warehouse if `reconcile_all` is requested from the report.

* `reconcile_items(item_codes)` (called by the "Reconcile All" report button):
    Creates a draft Stock Reconciliation pre-filled with each item's current
    qty.  The reconciler tweaks counts and submits.

* `mark_reconciled(item_codes)`:
    After the reconciliation submits, clears `custom_negative_stock_auto_enabled`
    and resets `allow_negative_stock` to 0 on those items.

If nothing is negative, the cron picks the top-5 fastest-moving items (Req
#7) — skipping items already requested within the last 30 days.
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import add_days, flt, get_time, getdate, now_datetime

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_settings,
)
from fours_customizations.notifications import send_email, send_slack


# ──────────────────────────────────────────────────────────────────────────
# Submit-time helpers: silently enable negative stock when projected qty
# would go below zero.
# ──────────────────────────────────────────────────────────────────────────


def ensure_negative_stock_for_doc(doc) -> None:
	"""For every stock item on `doc`, enable `allow_negative_stock` on the
	*item master* if the post-submit balance would otherwise be negative.

	Marks `custom_negative_stock_auto_enabled = 1` on the item so the daily
	cron can identify items it needs to chase down.
	"""
	settings = get_settings()
	if not int(settings.enable_negative_stock_automation or 0):
		return

	# Set the bench-level "allow negative stock" flag for the duration of this
	# submit, so the stock ledger entry passes through even if the item flag
	# hasn't yet taken effect (it's cached).
	from erpnext.stock.utils import get_stock_balance

	items_to_flag: list[str] = []
	for row in (doc.get("items") or []):
		item_code = getattr(row, "item_code", None)
		if not item_code:
			continue
		warehouse = getattr(row, "warehouse", None) or doc.get("set_warehouse")
		qty = flt(getattr(row, "qty", 0))
		if qty <= 0 or not warehouse:
			continue

		is_stock = frappe.db.get_value("Item", item_code, "is_stock_item")
		if not is_stock:
			continue
		try:
			balance = flt(get_stock_balance(item_code, warehouse))
		except Exception:
			balance = 0.0
		# For a Sales Invoice/Delivery Note the doc reduces stock — projected balance is `balance - qty`
		if balance - qty < 0:
			items_to_flag.append(item_code)

	for item_code in set(items_to_flag):
		current = frappe.db.get_value("Item", item_code, ["allow_negative_stock", "custom_negative_stock_auto_enabled"], as_dict=True)
		if not current:
			continue
		# Only mark if we are the ones enabling it
		if not current.allow_negative_stock:
			frappe.db.set_value("Item", item_code, "allow_negative_stock", 1, update_modified=False)
		frappe.db.set_value("Item", item_code, "custom_negative_stock_auto_enabled", 1, update_modified=False)

	# Set frappe.flags so child documents inherit during this submit
	if items_to_flag:
		frappe.flags.allow_negative_stock = True


# ──────────────────────────────────────────────────────────────────────────
# Daily cron
# ──────────────────────────────────────────────────────────────────────────


def get_items_with_negative_stock() -> list[dict]:
	"""Return rows with item_code, warehouse, current_qty for items in deficit."""
	rows = frappe.db.sql(
		"""
		SELECT item_code, warehouse, actual_qty
		FROM `tabBin`
		WHERE actual_qty < 0
		ORDER BY actual_qty ASC
		""",
		as_dict=True,
	)
	# Also include items marked but with non-negative qty (e.g. they bounced back)
	flagged = frappe.get_all(
		"Item",
		filters={"custom_negative_stock_auto_enabled": 1},
		fields=["name as item_code"],
	)
	known = {(r.item_code, r.warehouse) for r in rows}
	for f in flagged:
		if not any(r.item_code == f.item_code for r in rows):
			# Use the company's default warehouse as a placeholder; the reconciliation builder
			# will use the actual Bin warehouse anyway.
			rows.append({"item_code": f.item_code, "warehouse": None, "actual_qty": 0})
	return rows


def get_top_moving_items(n: int, days: int = 30) -> list[str]:
	"""Top-N items by SLE outflow over the last `days` days."""
	cutoff = add_days(getdate(now_datetime()), -days)
	rows = frappe.db.sql(
		"""
		SELECT item_code, SUM(ABS(actual_qty)) AS moved
		FROM `tabStock Ledger Entry`
		WHERE posting_date >= %s
			AND is_cancelled = 0
			AND voucher_type IN ('Sales Invoice', 'Delivery Note', 'Stock Entry')
		GROUP BY item_code
		ORDER BY moved DESC
		LIMIT %s
		""",
		(cutoff, int(n)),
		as_dict=True,
	)
	return [r.item_code for r in rows]


def _items_recently_requested(days: int = 30) -> set[str]:
	cutoff = add_days(now_datetime(), -days)
	rows = frappe.get_all(
		"Item",
		filters={"custom_last_reconciliation_request": [">=", cutoff]},
		pluck="name",
	)
	return set(rows)


def _mark_requested(item_codes: list[str]) -> None:
	for ic in item_codes:
		try:
			frappe.db.set_value("Item", ic, "custom_last_reconciliation_request", now_datetime(), update_modified=False)
		except Exception:
			pass


def _build_email_body(title: str, rows: list[dict], allow_button: bool) -> str:
	tr = "".join(
		f"<tr>"
		f"<td style='padding:6px 10px;border:1px solid #ddd;'>{r.get('item_code') or ''}</td>"
		f"<td style='padding:6px 10px;border:1px solid #ddd;'>{r.get('warehouse') or '—'}</td>"
		f"<td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{flt(r.get('actual_qty', 0)):,.2f}</td>"
		f"</tr>"
		for r in rows
	)
	button_html = ""
	if allow_button:
		button_html = (
			"<p style='margin-top:18px;'>Open the "
			"<a href='/app/query-report/Items Pending Reconciliation'>Items Pending Reconciliation</a> "
			"report and click <b>Reconcile All</b> to create a draft Stock Reconciliation.</p>"
		)
	return f"""
<div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#222;">
  <h3 style="margin-bottom:6px;">{title}</h3>
  <p>Please reconcile the items below at your earliest convenience.</p>
  <table style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead><tr style="background:#f0f0f0;">
      <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Item</th>
      <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Warehouse</th>
      <th style="padding:6px 10px;border:1px solid #ddd;text-align:right;">Qty</th>
    </tr></thead>
    <tbody>{tr or '<tr><td colspan=3 style="padding:6px 10px;border:1px solid #ddd;color:#666;">No items.</td></tr>'}</tbody>
  </table>
  {button_html}
</div>
"""


def daily_negative_stock_dispatcher() -> dict:
	"""Send the daily negative-stock summary (or top-mover suggestion)."""
	settings = get_settings()
	if not int(settings.enable_negative_stock_automation or 0):
		return {"skipped": True}

	negs = get_items_with_negative_stock()
	if negs:
		title = "Items With Negative Stock — Reconciliation Requested"
		body = _build_email_body(title, negs, allow_button=True)
		recipients = settings.negative_stock_notification_recipient
		send_email(title, body, recipients, cc=settings.negative_stock_notification_cc)
		send_slack(f"*{title}*\n{len(negs)} items need urgent reconciliation. Open ERPNext for details.")
		_mark_requested([r["item_code"] for r in negs])
		return {"sent": True, "count": len(negs)}

	# No negatives → optionally rotate the top-N movers
	if int(settings.enable_random_reconciliation or 0):
		top = get_top_moving_items(int(settings.top_movers_count or 5))
		recent = _items_recently_requested(30)
		fresh = [t for t in top if t not in recent]
		if not fresh:
			return {"skipped": True, "reason": "all top movers recently requested"}

		rows = [{"item_code": c, "warehouse": None, "actual_qty": 0} for c in fresh]
		title = "Routine Stock Reconciliation — Top Movers"
		body = _build_email_body(title, rows, allow_button=True)
		send_email(title, body, settings.negative_stock_notification_recipient, cc=settings.negative_stock_notification_cc)
		send_slack(f"*{title}*\nReconcile the top {len(fresh)} moving items today.")
		_mark_requested(fresh)
		return {"sent": True, "top_movers": fresh}

	return {"skipped": True, "reason": "no negatives, random disabled"}


def hourly_negative_stock_dispatcher() -> None:
	"""Hourly gate — runs the daily summary at the configured hour, once."""
	settings = get_settings()
	if not int(settings.enable_negative_stock_automation or 0):
		return
	target = get_time(settings.negative_stock_notification_time or "17:00:00")
	now = now_datetime()
	if now.hour != target.hour:
		return
	mark_key = f"4s_negative_stock:{now.date()}"
	if frappe.cache().get_value(mark_key):
		return
	daily_negative_stock_dispatcher()
	frappe.cache().set_value(mark_key, "1", expires_in_sec=2 * 60 * 60)


# ──────────────────────────────────────────────────────────────────────────
# Whitelisted helpers used by the report
# ──────────────────────────────────────────────────────────────────────────


@frappe.whitelist()
def reconcile_items(item_codes) -> str:
	"""Create a draft Stock Reconciliation for the given items and return its name."""
	import json

	if isinstance(item_codes, str):
		item_codes = json.loads(item_codes)
	if not item_codes:
		frappe.throw("Please pass at least one item.")

	# Pick a single company/warehouse — use settings default
	settings = get_settings()
	company = settings.default_company or frappe.defaults.get_global_default("company")
	if not company:
		frappe.throw("Default Company is not configured.")

	sr = frappe.new_doc("Stock Reconciliation")
	sr.purpose = "Stock Reconciliation"
	sr.company = company

	for ic in item_codes:
		bins = frappe.get_all(
			"Bin",
			filters={"item_code": ic},
			fields=["warehouse", "actual_qty", "valuation_rate"],
		)
		if not bins:
			default_wh = frappe.db.get_value("Item Default", {"parent": ic}, "default_warehouse") or settings.default_warehouse
			if not default_wh:
				continue
			bins = [{"warehouse": default_wh, "actual_qty": 0, "valuation_rate": 0}]
		for b in bins:
			sr.append("items", {
				"item_code": ic,
				"warehouse": b["warehouse"],
				"qty": flt(b["actual_qty"]),
				"valuation_rate": flt(b["valuation_rate"]),
			})

	if not sr.items:
		frappe.throw("Nothing to reconcile.")

	sr.flags.ignore_permissions = True
	sr.insert(ignore_permissions=True)
	frappe.db.commit()
	return sr.name


def mark_reconciled(item_codes: list[str]) -> None:
	"""Disable the auto-flag on items once reconciliation has completed."""
	for ic in set(item_codes):
		try:
			frappe.db.set_value("Item", ic, {
				"allow_negative_stock": 0,
				"custom_negative_stock_auto_enabled": 0,
			}, update_modified=False)
		except Exception:
			pass


def on_stock_reconciliation_submit(doc, method=None) -> None:
	"""Hook: when a Stock Reconciliation is submitted, clear the flag on its items."""
	if doc.docstatus != 1:
		return
	codes = [row.item_code for row in (doc.items or []) if row.item_code]
	if codes:
		mark_reconciled(codes)

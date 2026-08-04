"""
price_adjustment.py — Fours Customizations
==========================================

Correcting the price of an invoice **after the goods have gone out**.

**Why this exists**

Once a Delivery Note is submitted the invoice can no longer be cancelled — the
stock has left the store, and reversing the sale while the goods stay out is the
exact failure this app now guards against (see ``sales_chain_integrity``). But
prices do genuinely need correcting after delivery, and with no sanctioned way
to do it the accounts team reached for the only lever they had: cancel the
invoice and re-issue it. That is what tore the books away from the store.

So the fix is not another approval queue — an approval nobody services just
manufactures workarounds. It is a *bounded* action that needs no approval at
all inside its limits:

  * the customer was **overcharged** → a Credit Note for the difference;
  * the customer was **undercharged** → a supplementary Sales Invoice for the
    difference.

Either way the original invoice stands untouched as evidence, no stock moves,
and the correction carries its own reason and audit trail. Fraud has to leave a
record rather than erase one.

**The limits**

Every limit lives in *Four S Industries Settings → Price Adjustment* and can be
switched off by blanking it. Breaching any of them does not queue the request —
it refuses the action and names the limit, and only the configured approver role
can carry it out. That keeps the owner's involvement rare enough to actually
happen:

  * invoice older than N days;
  * adjustment larger than a fixed amount;
  * adjustment larger than a percentage of the invoice;
  * new price below the item's buying (valuation) rate;
  * adjustment that would leave the customer holding a net credit balance.

Plus maker-checker: whoever submitted the invoice may not adjust it.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import flt, fmt_money, getdate, nowdate, today

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_settings,
)

ROUNDING = 0.005


# ── configuration ────────────────────────────────────────────────────────────

def _settings():
	try:
		return get_settings()
	except Exception:
		return None


def _limit(field, default=None):
	"""A numeric limit from settings. 0 / blank means the limit is switched off."""
	settings = _settings()
	value = settings.get(field) if settings else None
	if value in (None, ""):
		return default
	return value


def _flag(field, default=1):
	settings = _settings()
	value = settings.get(field) if settings else None
	return default if value in (None, "") else int(value)


def _approver_role():
	return _limit("adjustment_approver_role") or None


def _user_is_approver(user=None):
	role = _approver_role()
	if not role:
		# No approver configured — nobody can exceed the limits, which fails
		# closed rather than silently letting everything through.
		return False
	return role in frappe.get_roles(user or frappe.session.user)


# ── costing / balances ───────────────────────────────────────────────────────

def _buying_rate(si_item) -> float:
	"""What the goods on this row actually cost us.

	Prefers the incoming rate recorded on the Delivery Note that shipped them,
	which is what the stock ledger actually consumed; falls back to the item's
	valuation rate.
	"""
	if si_item.get("dn_detail"):
		rate = frappe.db.get_value("Delivery Note Item", si_item.get("dn_detail"), "incoming_rate")
		if flt(rate):
			return flt(rate)

	for rate in frappe.get_all(
		"Delivery Note Item",
		filters={"si_detail": si_item.get("name"), "docstatus": 1},
		pluck="incoming_rate",
	):
		if flt(rate):
			return flt(rate)

	return flt(frappe.db.get_value("Item", si_item.get("item_code"), "valuation_rate"))


def _draft_delivery_note(invoice_name: str) -> str | None:
	"""A still-draft Delivery Note raised for this invoice, if any."""
	dn_names = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_invoice": invoice_name},
		pluck="parent",
		distinct=True,
	)
	if not dn_names:
		return None
	return frappe.db.get_value(
		"Delivery Note", {"name": ["in", dn_names], "docstatus": 0, "is_return": 0}, "name"
	)


def _customer_balance(customer, company) -> float:
	"""Signed receivable balance: positive means the customer owes us."""
	value = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(gle.debit - gle.credit), 0)
		FROM `tabGL Entry` gle
		INNER JOIN `tabAccount` a ON a.name = gle.account
		WHERE gle.party_type = 'Customer'
		  AND gle.party = %(customer)s
		  AND gle.company = %(company)s
		  AND gle.is_cancelled = 0
		  AND a.account_type = 'Receivable'
		""",
		{"customer": customer, "company": company},
	)
	return flt(value[0][0]) if value else 0.0


# ── effective price after corrections ────────────────────────────────────────

def effective_rates(invoice_name: str) -> dict:
	"""``{Sales Invoice Item row: unit price actually charged}`` for *invoice_name*.

	The rate printed on the invoice stops being the truth the moment a price
	correction is raised against it. Anything that later has to credit the
	customer — above all a goods return — must use the corrected figure, or the
	original overcharge gets refunded twice: once by the correction and again by
	the return.

	Example: 10 units invoiced at 25,000, corrected down to 22,000 by a credit
	note of 30,000. If the goods then come back and the return credits the
	original 25,000, the customer is handed 280,000 against a 250,000 sale.
	"""
	si = frappe.get_doc("Sales Invoice", invoice_name)
	rates = {item.name: flt(item.rate) for item in si.items}

	adjustments = frappe.get_all(
		"Sales Invoice",
		filters={"custom_price_adjustment_for": invoice_name, "docstatus": 1},
		pluck="name",
	)
	if not adjustments:
		return rates

	for row in frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": ["in", adjustments], "custom_adjusts_si_item": ["is", "set"]},
		fields=["custom_adjusts_si_item", "qty", "rate"],
	):
		target = row.custom_adjusts_si_item
		if target not in rates:
			continue
		# A credit note carries negative quantities at the reduction rate; a
		# supplementary invoice positive quantities at the increase rate. The
		# sign of the quantity is therefore the direction of the correction.
		rates[target] += flt(row.rate) * (1 if flt(row.qty) > 0 else -1)

	return rates


def outstanding_qty(invoice_name: str) -> dict:
	"""``{Sales Invoice Item row: units the customer still holds}``.

	A price correction may only re-price goods the customer actually still has.
	Units already handed back were credited at the price in force when they came
	back, and that transaction is closed. Re-pricing them again would credit the
	difference a second time on goods that are already sitting in the store.
	"""
	si = frappe.get_doc("Sales Invoice", invoice_name)
	held = {item.name: flt(item.qty) for item in si.items}

	# Goods returns are the credit notes raised against this invoice that are not
	# price corrections; their rows carry the invoice row they reverse.
	returns = frappe.get_all(
		"Sales Invoice",
		filters={
			"return_against": invoice_name,
			"docstatus": 1,
			"custom_price_adjustment_for": ["is", "not set"],
		},
		pluck="name",
	)
	if not returns:
		return held

	for row in frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": ["in", returns], "sales_invoice_item": ["is", "set"]},
		fields=["sales_invoice_item", "qty"],
	):
		if row.sales_invoice_item in held:
			held[row.sales_invoice_item] -= abs(flt(row.qty))

	return {row: max(qty, 0.0) for row, qty in held.items()}


# ── the dialog's data ────────────────────────────────────────────────────────

@frappe.whitelist()
def get_adjustment_context(sales_invoice: str) -> dict:
	"""Rows and limits for the Adjust Price dialog."""
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	si.check_permission("read")

	# Show the price actually in force. If this invoice has been corrected before,
	# the rate on the row is history and editing against it would compound wrongly.
	in_force = effective_rates(si.name)

	rows = []
	for item in si.items:
		current = flt(in_force.get(item.name, item.rate))
		rows.append({
			"item_row": item.name,
			"idx": item.idx,
			"item_code": item.item_code,
			"item_name": item.item_name,
			"qty": flt(item.qty),
			"uom": item.uom,
			"invoiced_rate": flt(item.rate),
			"current_rate": current,
			"amount": current * flt(item.qty),
			"buying_rate": _buying_rate(item),
		})

	return {
		"sales_invoice": si.name,
		"customer": si.customer,
		"customer_name": si.customer_name,
		"currency": si.currency,
		"grand_total": flt(si.grand_total),
		"posting_date": str(si.posting_date),
		"items": rows,
		"enabled": bool(_flag("enable_price_adjustment", 1)),
		"min_reason_length": int(flt(_limit("adjustment_min_reason_length", 50))),
		"is_approver": _user_is_approver(),
		"approver_role": _approver_role(),
		"limits": {
			"max_age_days": flt(_limit("adjustment_max_age_days", 0)),
			"max_amount": flt(_limit("adjustment_max_amount", 0)),
			"max_percent": flt(_limit("adjustment_max_percent", 0)),
			"block_below_cost": bool(_flag("adjustment_block_below_cost", 1)),
			"block_net_credit": bool(_flag("adjustment_block_net_credit", 1)),
		},
	}


# ── the limits ───────────────────────────────────────────────────────────────

def _evaluate_limits(si, changes, total_delta) -> list[str]:
	"""Return a human-readable list of the limits this adjustment breaches."""
	breaches = []
	currency = si.currency

	max_age = flt(_limit("adjustment_max_age_days", 0))
	if max_age:
		age = (getdate(nowdate()) - getdate(si.posting_date)).days
		if age > max_age:
			breaches.append(
				_("the invoice is {0} days old (limit is {1})").format(age, int(max_age))
			)

	max_amount = flt(_limit("adjustment_max_amount", 0))
	if max_amount and abs(total_delta) > max_amount:
		breaches.append(
			_("the adjustment is {0} (limit is {1})").format(
				fmt_money(abs(total_delta), currency=currency),
				fmt_money(max_amount, currency=currency),
			)
		)

	max_percent = flt(_limit("adjustment_max_percent", 0))
	if max_percent and flt(si.grand_total):
		pct = abs(total_delta) / flt(si.grand_total) * 100
		if pct > max_percent:
			breaches.append(
				_("the adjustment is {0}% of the invoice (limit is {1}%)").format(
					round(pct, 1), round(max_percent, 1)
				)
			)

	if _flag("adjustment_block_below_cost", 1):
		for change in changes:
			cost = flt(change["buying_rate"])
			if cost and flt(change["new_rate"]) < cost:
				breaches.append(
					_("{0} would be priced at {1}, below its buying price of {2}").format(
						change["item_code"],
						fmt_money(change["new_rate"], currency=currency),
						fmt_money(cost, currency=currency),
					)
				)

	if _flag("adjustment_block_net_credit", 1) and total_delta < 0:
		balance = _customer_balance(si.customer, si.company)
		if balance + total_delta < -ROUNDING:
			breaches.append(
				_("it would leave {0} with a credit balance of {1}").format(
					si.customer,
					fmt_money(abs(balance + total_delta), currency=currency),
				)
			)

	return breaches


def _check_maker_checker(si):
	if not _flag("adjustment_block_self_approval", 1):
		return
	submitter = frappe.db.get_value(
		"Version", {"ref_doctype": "Sales Invoice", "docname": si.name}, "owner"
	) or si.owner
	if frappe.session.user == submitter and not _user_is_approver():
		frappe.throw(
			_(
				"You raised {0}, so you cannot also adjust its price. "
				"Ask a colleague, or someone with the {1} role, to make the correction."
			).format(si.name, _approver_role() or _("approver")),
			title=_("Maker-checker"),
		)


# ── the adjustment ───────────────────────────────────────────────────────────

@frappe.whitelist()
def create_price_adjustment(sales_invoice: str, rows, reason: str) -> dict:
	"""Raise a Credit Note (overcharge) or supplementary invoice (undercharge)
	for the difference between the invoiced price and the corrected one.

	``rows`` is a list of ``{"item_row": <Sales Invoice Item name>,
	"new_rate": <corrected unit price>}``.
	"""
	if isinstance(rows, str):
		rows = json.loads(rows)

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	si.check_permission("submit")

	if not _flag("enable_price_adjustment", 1):
		frappe.throw(_("Price adjustment is switched off in Four S Industries Settings."))
	if si.docstatus != 1:
		frappe.throw(_("{0} is not submitted.").format(si.name))
	if si.get("is_return"):
		frappe.throw(_("{0} is itself a credit note; adjust the original invoice.").format(si.name))
	if si.get("is_consolidated"):
		frappe.throw(_("{0} is a consolidated POS invoice and cannot be adjusted this way.").format(si.name))

	# Nothing has left the store yet, so there is nothing to correct after the
	# fact — the invoice can still simply be cancelled and re-raised at the right
	# price. Adjusting here would leave a credit note against goods that never
	# went out.
	draft_dn = _draft_delivery_note(si.name)
	if draft_dn:
		frappe.throw(
			_(
				"Delivery Note {0} is still in draft, so nothing has left the store yet. "
				"Cancel this invoice and raise it again at the correct price instead of "
				"adjusting it."
			).format(draft_dn),
			title=_("Nothing delivered yet"),
		)

	min_len = int(flt(_limit("adjustment_min_reason_length", 50)))
	reason = (reason or "").strip()
	if len(reason) < min_len:
		frappe.throw(
			_("Please give a reason of at least {0} characters explaining this price change "
			  "(you wrote {1}).").format(min_len, len(reason)),
			title=_("Reason required"),
		)

	_check_maker_checker(si)

	items_by_row = {item.name: item for item in si.items}
	# Corrections compound. The baseline is the price in force *now*, not the one
	# first printed on the invoice — measuring from the original would re-credit
	# the whole difference on every subsequent correction (1000 → 950 → 900 would
	# credit 50 then 100, handing back 150 when the customer owes 100 less).
	in_force = effective_rates(si.name)
	held = outstanding_qty(si.name)
	changes, total_delta = [], 0.0
	for row in rows:
		item = items_by_row.get(row.get("item_row"))
		if not item:
			frappe.throw(_("Row {0} does not belong to {1}.").format(row.get("item_row"), si.name))
		new_rate = flt(row.get("new_rate"))
		if new_rate < 0:
			frappe.throw(_("A price cannot be negative ({0}).").format(item.item_code))
		current_rate = flt(in_force.get(item.name, item.rate))
		delta_rate = new_rate - current_rate
		if abs(delta_rate) < ROUNDING:
			continue

		qty = flt(held.get(item.name, item.qty))
		if qty <= 0:
			frappe.throw(
				_("{0} has already been returned in full, so there is no longer a "
				  "price to correct on it. Adjust the credit note instead.").format(item.item_code),
				title=_("Nothing left to re-price"),
			)

		delta = delta_rate * qty
		total_delta += delta
		changes.append({
			"item": item,
			"item_code": item.item_code,
			"qty": qty,
			"old_rate": current_rate,
			"new_rate": new_rate,
			"delta_rate": delta_rate,
			"delta": delta,
			"buying_rate": _buying_rate(item),
		})

	if not changes:
		frappe.throw(_("No price was changed."))

	# Mixing directions in one document would net off into a single figure and
	# hide what actually happened, so they are refused.
	if any(c["delta"] > 0 for c in changes) and any(c["delta"] < 0 for c in changes):
		frappe.throw(
			_("Some prices go up and others go down. Please make them as two separate "
			  "adjustments so each correction is recorded on its own.")
		)

	breaches = _evaluate_limits(si, changes, total_delta)
	if breaches and not _user_is_approver():
		frappe.throw(
			_(
				"""
<div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#222;">
  <p style="font-size:14px;"><b>This adjustment is outside the limits you can approve.</b></p>
  <p>It was refused because:</p>
  <ul>{0}</ul>
  <p>Someone with the <b>{1}</b> role can make this correction. Nothing has been changed.</p>
</div>
"""
			).format(
				"".join(f"<li>{b}</li>" for b in breaches),
				_approver_role() or _("approver"),
			),
			title=_("Approval required"),
		)

	if total_delta < 0:
		doc = _build_credit_note(si, changes, reason)
	else:
		doc = _build_supplementary_invoice(si, changes, reason)

	_notify(si, doc, changes, total_delta, reason, breaches)

	return {
		"name": doc.name,
		"doctype": "Sales Invoice",
		"is_return": int(doc.is_return),
		"total_adjustment": total_delta,
		"overridden": bool(breaches),
	}


def _apply_common(target, si, reason):
	target.company = si.company
	target.customer = si.customer
	target.currency = si.currency
	target.conversion_rate = flt(si.conversion_rate) or 1.0
	target.selling_price_list = si.selling_price_list
	target.price_list_currency = si.price_list_currency
	target.plc_conversion_rate = flt(si.plc_conversion_rate) or 1.0
	target.cost_center = si.get("cost_center")
	target.set_posting_time = 1
	target.posting_date = today()
	target.update_stock = 0
	target.is_pos = 0
	target.remarks = f"Price adjustment against {si.name}. Reason: {reason}"
	target.custom_price_adjustment_for = si.name
	# The rate on these rows is the *difference*, not a selling price. Left to
	# itself ERPNext would fetch the price list rate and derive a discount to
	# reach it — posting the full original value to revenue and the balance to
	# discounts. The net would still be right, but revenue and discount turnover
	# would both be inflated many times over, which is precisely the reporting
	# this feature exists to keep clean.
	target.ignore_pricing_rule = 1
	# No goods move on a price-only correction, so the automation must not raise
	# a Delivery Note (or a Delivery Note Return) for this document.
	target.flags.skip_delivery_note = True


def _append_row(target, si, change, qty, rate):
	income_account = _limit("adjustment_income_account")
	item = change["item"]
	row = target.append("items", {
		"item_code": item.item_code,
		"item_name": item.item_name,
		"description": item.description,
		"uom": item.uom,
		"conversion_factor": flt(item.conversion_factor) or 1.0,
		"qty": qty,
		"rate": rate,
		"cost_center": item.get("cost_center"),
		"warehouse": item.get("warehouse"),
		"custom_adjusts_si_item": item.name,
	})
	if income_account:
		row.income_account = income_account
	elif item.get("income_account"):
		row.income_account = item.income_account

	# Pin the rate so nothing re-derives it as "list price less a discount".
	row.price_list_rate = rate
	row.base_price_list_rate = flt(rate) * flt(target.conversion_rate or 1.0)
	row.discount_percentage = 0
	row.discount_amount = 0
	row.margin_type = ""
	row.margin_rate_or_amount = 0
	row.rate_with_margin = 0
	row.base_rate_with_margin = 0
	return row


def _build_credit_note(si, changes, reason):
	"""Customer was overcharged — credit the difference back."""
	credit = frappe.new_doc("Sales Invoice")
	_apply_common(credit, si, reason)
	credit.is_return = 1
	# Deliberately NOT return_against. ERPNext reads a return's quantities as goods
	# physically coming back, and these rows carry the full invoiced quantity — so
	# linking it would mark the whole line as returned and make the store's later,
	# genuine return impossible to post. Nothing came back here; only the price
	# changed. The audit link is custom_price_adjustment_for, set in _apply_common.

	for change in changes:
		# A credit note carries negative quantities; the rate is the per-unit
		# reduction, so the row's value is exactly the amount being credited.
		_append_row(credit, si, change, -flt(change["qty"]), abs(flt(change["delta_rate"])))

	credit.flags.ignore_permissions = True
	credit.insert()
	credit.submit()
	return credit


def _build_supplementary_invoice(si, changes, reason):
	"""Customer was undercharged — bill the difference."""
	extra = frappe.new_doc("Sales Invoice")
	_apply_common(extra, si, reason)
	extra.is_return = 0
	# The credit decision was made when the goods went out on the original
	# invoice; this document only corrects its price, so the new-sale credit
	# gate does not apply. The adjustment limits above are what bound it.
	extra.flags.allow_unpaid_price_adjustment = True

	for change in changes:
		_append_row(extra, si, change, flt(change["qty"]), flt(change["delta_rate"]))

	extra.flags.ignore_permissions = True
	extra.insert()
	extra.submit()
	return extra


# ── notification ─────────────────────────────────────────────────────────────

def _notify(si, doc, changes, total_delta, reason, breaches):
	"""Post every adjustment to Slack. Never let this break the correction."""
	if not _flag("adjustment_notify_slack", 1):
		return
	try:
		from fours_customizations.notifications import send_slack

		kind = "Credit Note" if doc.is_return else "Supplementary Invoice"
		lines = "\n".join(
			f'  • {c["item_code"]}: {fmt_money(c["old_rate"], currency=si.currency)} → '
			f'{fmt_money(c["new_rate"], currency=si.currency)} × {c["qty"]:g}'
			for c in changes
		)
		override = (
			f"\n*Approver override:* {'; '.join(breaches)}" if breaches else ""
		)
		send_slack(
			f"*Price adjusted after delivery*\n"
			f"*Invoice:* {si.name} ({si.customer})\n"
			f"*{kind}:* {doc.name}\n"
			f"*Net change:* {fmt_money(total_delta, currency=si.currency)}\n"
			f"*By:* {frappe.session.user}\n"
			f"{lines}\n"
			f"*Reason:* {reason}{override}"
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S Price Adjustment: Slack notify failed")

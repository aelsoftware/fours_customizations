"""
Sales Order Handler — Fours Customizations
==========================================
Hooks fired on Sales Order:

  before_submit
    • Checks whether the customer carries any unreconciled receivable balance
      across ALL receivable accounts for this company.  If so, submission is
      blocked with a formal notice addressed to the logged-in user.

  on_submit
    • If custom_include_payment=1, validates custom_payments vs. grand_total
      (accounting for change) and creates one submitted Payment Entry per row.

  on_cancel
    • Cancels every Payment Entry that was created from this Sales Order.
"""

import frappe
from frappe.utils import flt, fmt_money, nowdate
from erpnext.accounts.party import get_party_account

# ── helpers ───────────────────────────────────────────────────────────────────

def _automation_enabled(company: str) -> bool:
	return bool(frappe.db.get_value("Company", company, "enable_selling_automations"))


def _get_session_first_name() -> str:
	"""Return the first name of the currently logged-in user."""
	full_name = (
		frappe.db.get_value("User", frappe.session.user, "full_name") or ""
	).strip()
	if not full_name:
		return "Esteemed Colleague"
	return full_name.split()[0]


def _get_customer_outstanding(customer: str, company: str) -> list[dict]:
	"""
	Return a list of dicts describing every receivable account in which
	this customer carries a positive (debit-heavy) balance.

	Each dict: { account, balance, currency }
	"""
	rows = frappe.db.sql(
		"""
		SELECT
			gle.account,
			SUM(gle.debit - gle.credit)   AS balance,
			a.account_currency            AS currency
		FROM `tabGL Entry` gle
		INNER JOIN `tabAccount` a
			ON a.name = gle.account
		WHERE
			gle.party_type  = 'Customer'
			AND gle.party   = %(customer)s
			AND gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND a.account_type  = 'Receivable'
		GROUP BY gle.account, a.account_currency
		HAVING balance > 0.005
		ORDER BY balance DESC
		""",
		{"customer": customer, "company": company},
		as_dict=True,
	)
	return rows


def _build_debt_error(doc, outstanding_rows: list[dict], first_name: str) -> str:
	"""
	Compose a formal, judge-addressed HTML error message that names the
	customer, lists every outstanding account, and states the total due.
	"""
	customer_name  = doc.customer_name or doc.customer
	so_name        = doc.name
	company        = doc.company

	# ── grand total across all accounts (may be multi-currency; sum same-ccy) ──
	# Group by currency for a clean per-currency total
	by_currency: dict[str, float] = {}
	for r in outstanding_rows:
		by_currency[r.currency] = by_currency.get(r.currency, 0.0) + flt(r.balance)

	# ── account breakdown rows ──
	account_rows_html = ""
	for r in outstanding_rows:
		formatted = fmt_money(r.balance, currency=r.currency)
		account_rows_html += (
			f"<tr>"
			f"<td style='padding:6px 12px;border:1px solid #ddd;'>{r.account}</td>"
			f"<td style='padding:6px 12px;border:1px solid #ddd;text-align:right;"
			f"font-weight:600;color:#c0392b;'>{formatted}</td>"
			f"</tr>"
		)

	# ── per-currency total line(s) ──
	total_lines = " | ".join(
		f"<b>{fmt_money(amt, currency=ccy)}</b>"
		for ccy, amt in by_currency.items()
	)

	msg = f"""
<div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.7;color:#222;">

  <p style="font-size:15px;margin-bottom:4px;">
	<b>Respectfully,&nbsp;{first_name},</b>
  </p>

  <p style="margin-top:0;font-size:13px;color:#555;font-style:italic;">
	— A Formal Notice of Outstanding Debt —
  </p>

  <hr style="border:none;border-top:2px solid #c0392b;margin:8px 0 14px;">

  <p>
	It is most respectfully brought before Your Honour that the customer
	<b>{customer_name}</b> stands presently indebted to <b>{company}</b>
	with unresolved financial obligations that remain outstanding and
	unpaid in our books of account. The said obligations are recorded
	across the following receivable account(s):
  </p>

  <table style="border-collapse:collapse;width:100%;margin:10px 0 16px;font-size:13px;">
	<thead>
	  <tr style="background:#f0f0f0;">
		<th style="padding:7px 12px;border:1px solid #ddd;text-align:left;">Receivable Account</th>
		<th style="padding:7px 12px;border:1px solid #ddd;text-align:right;">Outstanding Balance</th>
	  </tr>
	</thead>
	<tbody>
	  {account_rows_html}
	</tbody>
	<tfoot>
	  <tr style="background:#fff5f5;">
		<td style="padding:7px 12px;border:1px solid #ddd;font-weight:700;">Total Due</td>
		<td style="padding:7px 12px;border:1px solid #ddd;text-align:right;
				   font-weight:700;color:#c0392b;font-size:14px;">{total_lines}</td>
	  </tr>
	</tfoot>
  </table>

  <p>
	{first_name}, it is the considered, firm, and unequivocal position of
	this establishment that extending a further Sales Order to a party
	who has not yet honoured their prior financial commitment would be
	<b>commercially imprudent</b>, <b>financially irresponsible</b>, and
	wholly contrary to the sound principles of prudent credit management.
	To do so would, in effect, reward the conduct of non-payment,
	undermine the financial integrity of <b>{company}</b>, and expose the
	business to compounding credit risk without justification.
  </p>

  <p>
	It is therefore most respectfully submitted that <b>{customer_name}</b>
	must first <b>settle in full</b> the total outstanding due amount of
	{total_lines} before any new order may be entertained, processed,
	or approved by this establishment.
  </p>

  <p style="background:#fff3cd;border-left:4px solid #f0a500;
			 padding:10px 14px;border-radius:3px;font-size:13px;">
	⚖️ &nbsp;The submission of Sales Order <b>{so_name}</b> has been
	<b>withheld</b> by the system, pending full resolution and settlement
	of the above-stated financial obligation.
  </p>

</div>
"""
	return msg


# ── hooks ─────────────────────────────────────────────────────────────────────

def before_submit(doc, method=None):
	"""Block SO submission if the customer has any outstanding receivable balance.

	Bypassed entirely when the customer's custom_allow_credit flag is checked —
	meaning the business has explicitly approved this customer for credit trading.
	"""
	allow_credit = frappe.db.get_value("Customer", doc.customer, "custom_allow_credit")
	if allow_credit:
		return  # Customer is approved for credit — skip debt check

	outstanding = _get_customer_outstanding(doc.customer, doc.company)
	if not outstanding:
		return  # All clear — no debt

	first_name = _get_session_first_name()
	msg = _build_debt_error(doc, outstanding, first_name)
	frappe.throw(msg, title="⚖️ Outstanding Debt — Submission Blocked")


def on_submit(doc, method=None):
	"""Entry point: validate payments and create Payment Entries."""
	if not flt(doc.custom_include_payment):
		return
	if not _automation_enabled(doc.company):
		return

	_validate_payments(doc)
	_create_payment_entries(doc)


def on_cancel(doc, method=None):
	"""Cancel all Payment Entries that were created for this Sales Order."""
	if not _automation_enabled(doc.company):
		return

	pes = frappe.get_all(
		"Payment Entry",
		filters={
			"reference_no": doc.name,
			"party_type": "Customer",
			"party": doc.customer,
			"payment_type": "Receive",
			"docstatus": 1,
		},
		pluck="name",
		order_by="creation desc",
	)

	for pe_name in pes:
		pe = frappe.get_doc("Payment Entry", pe_name)
		pe.cancel()
		frappe.msgprint(f"Payment Entry {pe_name} cancelled.", alert=True)


# ── validation ────────────────────────────────────────────────────────────────

def _validate_payments(doc):
	"""
	Strict validation rules:
	  • custom_payments must be non-empty
	  • every row must have mode_of_payment and account
	  • change_amount >= 0
	  • if change_amount > 0, custom_account_for_change_amount must be set
	  • sum(custom_payments.amount) − change_amount == grand_total  (2-decimal tolerance)
	  • change_amount < sum(custom_payments.amount)   (can't give more change than collected)
	"""
	if not doc.custom_payments:
		frappe.throw(
			"No payment rows found in <b>Payments</b> table. "
			"Add at least one payment method before submitting."
		)

	grand_total = flt(doc.grand_total, 2)
	change = flt(doc.custom_change_amount, 2)
	total_collected = flt(sum(flt(p.amount) for p in doc.custom_payments), 2)

	# ── row-level checks ──
	for i, p in enumerate(doc.custom_payments, start=1):
		if not p.mode_of_payment:
			frappe.throw(f"Payment row {i}: <b>Mode of Payment</b> is required.")
		if not p.account:
			frappe.throw(
				f"Payment row {i} ({p.mode_of_payment}): <b>Account</b> is missing. "
				"Check the Mode of Payment configuration."
			)
		if flt(p.amount) <= 0:
			frappe.throw(
				f"Payment row {i} ({p.mode_of_payment}): Amount must be greater than zero."
			)

	# ── change checks ──
	if change < 0:
		frappe.throw("Change amount cannot be negative.")

	if change >= total_collected:
		frappe.throw(
			f"Change amount ({change:,.2f}) must be less than the total collected "
			f"({total_collected:,.2f})."
		)

	if change > 0 and not doc.custom_account_for_change_amount:
		frappe.throw(
			"Change amount is set but <b>Account for Change Amount</b> is not configured."
		)

	# ── net payment check ──
	net_paid = flt(total_collected - change, 2)
	if net_paid != grand_total:
		frappe.throw(
			f"Payment mismatch:<br>"
			f"&nbsp;&nbsp;Collected &nbsp;&nbsp;&nbsp;: <b>{total_collected:,.2f}</b><br>"
			f"&nbsp;&nbsp;Change &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: <b>{change:,.2f}</b><br>"
			f"&nbsp;&nbsp;Net received : <b>{net_paid:,.2f}</b><br>"
			f"&nbsp;&nbsp;Order total &nbsp;: <b>{grand_total:,.2f}</b><br><br>"
			"Net received must equal the order total. "
			"Adjust the payment amounts or the change amount."
		)


# ── payment creation ──────────────────────────────────────────────────────────

def _effective_payments(doc) -> list[dict]:
	"""
	Return a list of {mode, account, amount} after deducting the change.

	Strategy:
	  1. Find the first row whose account matches custom_account_for_change_amount
	     and subtract as much change as that row can absorb.
	  2. If change is still unabsorbed (e.g. no matching account), keep deducting
	     from the last row(s) in reverse order.
	  3. Drop rows whose effective amount is <= 0.
	"""
	change_account = doc.custom_account_for_change_amount or ""
	remaining = flt(doc.custom_change_amount, 2)

	rows = [
		{"mode": p.mode_of_payment, "account": p.account, "amount": flt(p.amount, 2)}
		for p in doc.custom_payments
	]

	# Pass 1: absorb from matching account row(s)
	if remaining > 0 and change_account:
		for row in rows:
			if row["account"] == change_account and remaining > 0:
				absorb = min(row["amount"], remaining)
				row["amount"] = flt(row["amount"] - absorb, 2)
				remaining = flt(remaining - absorb, 2)

	# Pass 2: absorb any remainder from the last non-zero rows (reverse)
	if remaining > 0:
		for row in reversed(rows):
			if row["amount"] > 0 and remaining > 0:
				absorb = min(row["amount"], remaining)
				row["amount"] = flt(row["amount"] - absorb, 2)
				remaining = flt(remaining - absorb, 2)

	# Safety: if somehow remaining > 0, raise (should never happen after _validate_payments)
	if remaining > 0:
		frappe.throw(
			f"Could not fully distribute the change amount. "
			f"Unabsorbed change: {remaining:,.2f}. Please review payment rows."
		)

	return [r for r in rows if r["amount"] > 0]


def _create_payment_entries(doc):
	effective = _effective_payments(doc)
	if not effective:
		frappe.msgprint("No effective payment amounts to record.", alert=True)
		return

	receivable_account = get_party_account("Customer", doc.customer, doc.company)
	cost_center = frappe.get_cached_value("Company", doc.company, "cost_center")
	posting_date = doc.transaction_date or nowdate()
	created = []

	for row in effective:
		pe = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"posting_date": posting_date,
			"company": doc.company,
			"party_type": "Customer",
			"party": doc.customer,
			"party_name": doc.customer_name,
			"mode_of_payment": row["mode"],
			# Receivable account → gets credited (reduces what customer owes)
			"paid_from": receivable_account,
			"paid_from_account_currency": doc.currency,
			# Cash / bank account → gets debited (money lands here)
			"paid_to": row["account"],
			"paid_to_account_currency": doc.currency,
			"paid_amount": row["amount"],
			"received_amount": row["amount"],
			"source_exchange_rate": flt(doc.conversion_rate) or 1,
			"target_exchange_rate": flt(doc.conversion_rate) or 1,
			"reference_no": doc.name,
			"reference_date": posting_date,
			"cost_center": cost_center,
			"currency": doc.currency,
			"remarks": (
				f"Payment received against Sales Order {doc.name} "
				f"via {row['mode']}."
			),
		})

		pe.insert(ignore_permissions=True)
		pe.submit()
		created.append(pe.name)

	if created:
		label = "Payment Entries" if len(created) > 1 else "Payment Entry"
		frappe.msgprint(
			f"{label} {', '.join(created)} created for {doc.name}.",
			alert=True,
		)
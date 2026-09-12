"""
sales_chain_integrity.py — Fours Customizations
===============================================

Failsafe for the Sales Invoice ↔ Delivery Note pair.

**The failure this prevents**

A Sales Invoice was cancelled while its Delivery Note stayed *submitted*: the
accounts side saw a cancelled invoice (GL reversed, nothing in the customer's
ledger) while the stores side still had the stock deducted. Seven invoices
worth ~131.8M reached that state between Feb and Jul 2026 — including
SI-2627-039-3 / DN-2627-115 (MWIJUKA HOIMA TRIP).

The route in was ``delivery_note_handler._cancel_sales_invoice``: deleting a
draft Delivery Note cancels the invoice it belongs to, and it did so with
``flags.ignore_links = True`` — which switches off exactly the ERPNext check
that would otherwise refuse to cancel an invoice a submitted Delivery Note
still points at. Nothing verified that *another* Delivery Note for the same
invoice wasn't already submitted (the Sales Order teardown right next to it
always had that guard; the invoice path never did).

**The guard**

``validate_no_submitted_delivery_note`` is wired to Sales Invoice
``before_cancel``. Being a doc_event it fires on *every* cancellation route —
the desk Cancel button, the cancellation-requests app, the delivery-note
teardown, a bench script — and it is **not** silenced by ``ignore_links`` or
``ignore_permissions``, so it holds even where the native link check is
deliberately switched off. A ``frappe.throw`` here aborts the whole
transaction, so the invoice cannot come apart from its stock.

Return Delivery Notes are netted off: once the goods are back, the invoice is
free to cancel again.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

VOX_NANSANA_COMPANY = "Vox Lounge Nansana"
VOX_NANSANA_PAID_CANCEL_FLAG = "allow_vox_nansana_embedded_payment_cancel"
NATIVE_PAYMENT_CANCELLATION_SETTING = "allow_invoice_payment_unlink_on_cancel"


def submitted_delivery_notes_for_invoice(invoice_name: str) -> list[str]:
	"""Forward Delivery Notes that are still submitted and still holding stock
	for *invoice_name*.

	A note whose goods have come back (a submitted return points at it) no
	longer holds anything, so it is excluded — cancelling the invoice is
	legitimate at that point.
	"""
	if not invoice_name:
		return []

	dn_names = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_invoice": invoice_name},
		pluck="parent",
		distinct=True,
	)
	if not dn_names:
		return []

	live = frappe.get_all(
		"Delivery Note",
		filters={"name": ["in", dn_names], "is_return": 0, "docstatus": 1},
		pluck="name",
	)
	if not live:
		return []

	returned = set(
		frappe.get_all(
			"Delivery Note",
			filters={"return_against": ["in", live], "is_return": 1, "docstatus": 1},
			pluck="return_against",
		)
	)
	return sorted(name for name in live if name not in returned)


def invoice_amendment_chain(invoice_name: str) -> list[str]:
	"""*invoice_name* plus every invoice it was amended from, oldest last.

	A Delivery Note keeps pointing at the invoice it was built from, so after an
	amendment the note references an *earlier* name in the chain. Anything
	asking "is this invoice already delivered?" has to look at the whole chain
	or it will miss the note and deliver the same goods twice.
	"""
	chain, seen, current = [], set(), invoice_name
	while current and current not in seen:
		chain.append(current)
		seen.add(current)
		current = frappe.db.get_value("Sales Invoice", current, "amended_from")
	return chain


def invoice_descendants(invoice_names) -> dict[str, set]:
	"""{invoice: every invoice amended out of it, at any depth}.

	The forward counterpart of :func:`invoice_amendment_chain`. Repairing a
	wrongly-cancelled invoice means amending it and submitting the amendment,
	which lands under a *new* name — while the Delivery Note still references
	the cancelled one. Without following amendments forward, a repaired pair
	keeps reporting as broken and the fix looks like it did not work.
	"""
	descendants: dict[str, set] = {name: set() for name in invoice_names}
	# parent → the root(s) it descends from, so depth is resolved in one sweep
	roots_of = {name: {name} for name in invoice_names}
	frontier = list(invoice_names)
	seen = set(frontier)

	while frontier:
		children = frappe.get_all(
			"Sales Invoice",
			filters={"amended_from": ["in", frontier]},
			fields=["name", "amended_from"],
		)
		frontier = []
		for child in children:
			if child.name in seen:
				continue
			seen.add(child.name)
			roots = roots_of.get(child.amended_from, set())
			roots_of[child.name] = roots
			for root in roots:
				descendants[root].add(child.name)
			frontier.append(child.name)
	return descendants


def delivery_notes_covering_invoice(invoice_name: str) -> list[str]:
	"""Submitted forward Delivery Notes that already shipped this invoice —
	matched across its whole amendment chain."""
	chain = invoice_amendment_chain(invoice_name)
	if not chain:
		return []

	dn_names = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_invoice": ["in", chain]},
		pluck="parent",
		distinct=True,
	)
	if not dn_names:
		return []
	return frappe.get_all(
		"Delivery Note",
		filters={"name": ["in", dn_names], "is_return": 0, "docstatus": 1},
		pluck="name",
	)


def payments_against_invoice(invoice_name: str) -> list[dict]:
	"""Live money allocated to *invoice_name* — Payment Entries and Journal
	Entries that point at it, plus any POS payment taken on the invoice itself.

	Separate receipts survive invoice cancellation as unapplied customer
	credit when ERPNext unlinks them. Embedded POS payments belong to the
	invoice voucher and are reversed with its ledger entries. The site's
	cancellation policy decides which of these allocations block cancellation.
	"""
	if not invoice_name:
		return []

	allocated = []
	for row in frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": invoice_name, "docstatus": 1},
		fields=["parent", "allocated_amount"],
	):
		if flt(row.allocated_amount):
			allocated.append({
				"voucher_type": "Payment Entry",
				"voucher": row.parent,
				"amount": flt(row.allocated_amount),
			})

	for row in frappe.get_all(
		"Journal Entry Account",
		filters={"reference_type": "Sales Invoice", "reference_name": invoice_name, "docstatus": 1},
		fields=["parent", "credit_in_account_currency", "debit_in_account_currency"],
	):
		amount = flt(row.credit_in_account_currency) or flt(row.debit_in_account_currency)
		if amount:
			allocated.append({
				"voucher_type": "Journal Entry",
				"voucher": row.parent,
				"amount": amount,
			})

	# POS / "Include Payment" money banked on the invoice itself.
	paid_on_invoice = flt(
		frappe.db.get_value("Sales Invoice", invoice_name, "paid_amount")
	)
	if paid_on_invoice:
		allocated.append({
			"voucher_type": "Sales Invoice",
			"voucher": invoice_name,
			"amount": paid_on_invoice,
		})

	return allocated


def has_only_embedded_invoice_payment(doc) -> bool:
	"""True when the only live money on ``doc`` belongs to the POS invoice itself.

	A POS payment row is part of the Sales Invoice voucher: cancelling the
	invoice reverses both the sale and its cash/bank leg. Payment Entries and
	Journal Entries are separate vouchers, so this deliberately rejects even a
	zeroed live reference to either one.
	"""
	paid_amount = flt(doc.get("paid_amount"))
	if paid_amount <= 0:
		return False

	embedded_total = sum(
		flt(row.get("amount")) for row in (doc.get("payments") or []) if flt(row.get("amount")) > 0
	)
	if embedded_total <= 0 or abs(embedded_total - paid_amount) > 0.01:
		return False

	if frappe.get_all(
		"Payment Entry Reference",
		filters={
			"reference_doctype": "Sales Invoice",
			"reference_name": doc.name,
			"docstatus": 1,
		},
		pluck="name",
		limit=1,
	):
		return False

	if frappe.get_all(
		"Journal Entry Account",
		filters={
			"reference_type": "Sales Invoice",
			"reference_name": doc.name,
			"docstatus": 1,
		},
		pluck="name",
		limit=1,
	):
		return False

	allocated = payments_against_invoice(doc.name)
	return bool(allocated) and all(
		row["voucher_type"] == "Sales Invoice" and row["voucher"] == doc.name
		for row in allocated
	)


def can_auto_cancel_nansana_embedded_payment(
	doc, user=None, *, during_cancel=False
) -> bool:
	"""Whether this request may bypass the global paid-invoice cancellation guard.

	The exception is intentionally narrower than ordinary ERPNext cancellation:
	it applies only to a Vox Lounge Nansana POS invoice, only through a user in a
	configured Cancellation Request Settings auto-cancel role, and only when no
	Delivery Note or separate payment voucher exists.
	"""
	allowed_statuses = (1, 2) if during_cancel else (1,)
	if (
		doc.get("docstatus") not in allowed_statuses
		or doc.get("company") != VOX_NANSANA_COMPANY
		or not doc.get("is_pos")
		or doc.get("is_return")
	):
		return False

	from cancellation_requests.utils import can_user_auto_cancel, can_user_cancel_doctype

	user = user or frappe.session.user
	if not can_user_auto_cancel(user) or can_user_cancel_doctype(user, "Sales Invoice"):
		return False
	if not frappe.has_permission("Sales Invoice", ptype="read", doc=doc, user=user):
		return False

	if frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_invoice": doc.name},
		pluck="name",
		limit=1,
	):
		return False

	return has_only_embedded_invoice_payment(doc)


def validate_no_payment_allocated(doc, method=None):
	"""Sales Invoice ``before_cancel`` — apply the site's paid-invoice policy.

	Opted-in sites let ERPNext reverse embedded invoice payments and unlink
	Payment Entry allocations. The payment voucher and any allocations to
	other invoices remain intact; the released amount becomes customer credit.
	Journal Entries continue to require explicit resolution before cancelling.
	"""
	if doc.get("is_return"):
		return
	if doc.flags.get("allow_cancel_with_payment"):
		# Deliberate, audited override — the money has already been unwound.
		return
	if (
		frappe.flags.get(VOX_NANSANA_PAID_CANCEL_FLAG) == doc.name
		and can_auto_cancel_nansana_embedded_payment(doc, during_cancel=True)
	):
		# The request-scoped marker is set only by the cancellation-request
		# override. Rechecking company, role and payment ownership here prevents a
		# caller from using the marker to widen the exception. Keep the established
		# document flag too so the reason for the bypass remains explicit to later
		# hooks in this cancellation.
		doc.flags.allow_cancel_with_payment = True
		return

	allocated = payments_against_invoice(doc.name)
	if cint(frappe.conf.get(NATIVE_PAYMENT_CANCELLATION_SETTING)):
		# No company or role exception: authorization belongs to the calling
		# cancellation workflow. This hook runs with docstatus=2, before native
		# on_cancel reverses GL and unlinks receipts in the same transaction.
		unlink_payment_entries = frappe.get_single_value(
			"Accounts Settings", "unlink_payment_on_cancellation_of_invoice"
		)
		allocated = [
			row for row in allocated
			if not (
				(row["voucher_type"] == "Sales Invoice" and row["voucher"] == doc.name)
				or (row["voucher_type"] == "Payment Entry" and unlink_payment_entries)
			)
		]
	if not allocated:
		return

	total = sum(row["amount"] for row in allocated)
	lines = "".join(
		f'<li><b>{row["voucher_type"]}</b> '
		f'<a href="/app/{frappe.scrub(row["voucher_type"]).replace("_", "-")}/{row["voucher"]}">'
		f'{row["voucher"]}</a> — {frappe.utils.fmt_money(row["amount"], currency=doc.currency)}</li>'
		for row in allocated
	)
	frappe.throw(
		_(
			"""
<div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#222;">
  <p style="font-size:14px;"><b>This invoice cannot be cancelled — money has been received against it.</b></p>
  <p>{0} has already been taken against this invoice:</p>
  <ul>{1}</ul>
  <p>Cancelling now would reverse the sale but leave that money on the customer's
     account as an <b>unapplied advance</b> — a credit balance that did not come
     from any payment they made for something else.</p>
  <p><b>Do this instead:</b></p>
  <ol>
    <li>Wrong amount? Leave the invoice alone and raise a <b>Credit Note</b>
        (or use <b>Adjust Price</b>) for the difference.</li>
    <li>Genuinely cancelling the sale? Cancel or re-allocate the payment first,
        so the refund is recorded on its own, then cancel this invoice.</li>
  </ol>
</div>
"""
		).format(frappe.utils.fmt_money(total, currency=doc.currency), lines),
		title=_("Payment already received"),
	)


def validate_no_submitted_delivery_note(doc, method=None):
	"""Sales Invoice ``before_cancel`` — refuse to cancel while a submitted
	Delivery Note still holds the stock for this invoice."""
	if doc.get("is_return"):
		return
	if doc.flags.get("allow_cancel_with_delivery_note"):
		# Deliberate, audited override (see cancel_invoice_with_delivery_notes).
		return

	blocking = submitted_delivery_notes_for_invoice(doc.name)
	if not blocking:
		return

	links = ", ".join(
		f'<a href="/app/delivery-note/{name}"><b>{name}</b></a>' for name in blocking
	)
	frappe.throw(
		_(
			"""
<div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#222;">
  <p style="font-size:14px;"><b>This invoice cannot be cancelled — the goods are still out.</b></p>
  <p>Delivery Note {0} is still <b>submitted</b>, so the stock for this invoice has
     already left the store. Cancelling the invoice now would remove the sale from
     the customer's ledger while the stock stays deducted — the accounts and the
     store would disagree.</p>
  <p><b>Do this instead:</b></p>
  <ol>
    <li>If the goods came back — raise a <b>Delivery Note Return</b> against {0}
        (or cancel it), then cancel this invoice.</li>
    <li>If the goods did <b>not</b> come back — the invoice is correct and must
        stay. Fix the amount with a <b>Credit Note</b> (Sales Invoice Return)
        rather than cancelling.</li>
  </ol>
</div>
"""
		).format(links),
		title=_("Delivery Note still submitted"),
	)


def credit_notes_for_delivery_return(delivery_note: str) -> list[str]:
	"""Submitted credit notes raised off this Delivery Note Return."""
	if not delivery_note:
		return []
	names = frappe.get_all(
		"Sales Invoice Item",
		filters={"delivery_note": delivery_note},
		pluck="parent",
		distinct=True,
	)
	if not names:
		return []
	return frappe.get_all(
		"Sales Invoice",
		filters={"name": ["in", names], "docstatus": 1},
		pluck="name",
	)


def submitted_delivery_returns_for_credit_note(invoice_name: str) -> list[str]:
	"""Submitted Delivery Note Returns whose goods this credit note paid for."""
	if not invoice_name:
		return []
	names = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": invoice_name, "delivery_note": ["is", "set"]},
		pluck="delivery_note",
		distinct=True,
	)
	# A credit note raised for the price uplift on returned goods carries no row
	# level delivery note — linking one would distort the return's billing status
	# — so it names the return on the header instead.
	credits_return = frappe.db.get_value(
		"Sales Invoice", invoice_name, "custom_credits_delivery_return"
	)
	if credits_return:
		names = [*list(names or []), credits_return]
	if not names:
		return []
	return frappe.get_all(
		"Delivery Note",
		filters={"name": ["in", names], "is_return": 1, "docstatus": 1},
		pluck="name",
	)


def validate_no_submitted_delivery_return(doc, method=None):
	"""Sales Invoice ``before_cancel`` — refuse to cancel a credit note while the
	goods it credited are back in the store.

	The mirror of the fault this app was built to stop. There, an invoice was
	cancelled while its Delivery Note kept the stock out. Here, cancelling the
	credit note would re-bill a customer for goods already sitting back on the
	shelf — the same divergence between the store and the ledger, arrived at from
	the opposite direction.

	Price corrections are untouched by this: they credit no goods, so they carry
	no delivery note and stay freely cancellable.
	"""
	if not doc.get("is_return"):
		return
	if doc.flags.get("allow_cancel_with_delivery_return"):
		return

	blocking = submitted_delivery_returns_for_credit_note(doc.name)
	if not blocking:
		return

	links = ", ".join(
		f'<a href="/app/delivery-note/{name}"><b>{name}</b></a>' for name in blocking
	)
	frappe.throw(
		_(
			"""
<div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#222;">
  <p style="font-size:14px;"><b>This credit note cannot be cancelled — the goods are back in the store.</b></p>
  <p>Delivery Note Return {0} is still <b>submitted</b>, so the stock has already
     been taken back in.</p>
  <p>Cancelling this credit note would put the charge back on the customer for
     goods they have already returned — they would be billed for stock sitting on
     our own shelf.</p>
  <p><b>Do this instead:</b> cancel {0} first, then cancel this credit note.</p>
</div>
"""
		).format(links),
		title=_("Goods already returned"),
	)


def validate_no_credit_note_on_return(doc, method=None):
	"""Delivery Note ``before_cancel`` — refuse to cancel a goods return while the
	credit note it produced is still standing.

	Cancelling the return puts the goods back out to the customer. If the credit
	note stays submitted they keep both the goods and the money — the same
	divergence as the original fault, only pointing the other way. ERPNext's own
	link check is not enough here: the teardown paths in this app set
	``ignore_links``, which switches it off.
	"""
	if not doc.get("is_return"):
		return
	if doc.flags.get("allow_cancel_with_credit_note"):
		return

	blocking = credit_notes_for_delivery_return(doc.name)
	if not blocking:
		return

	links = ", ".join(
		f'<a href="/app/sales-invoice/{name}"><b>{name}</b></a>' for name in blocking
	)
	frappe.throw(
		_(
			"""
<div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#222;">
  <p style="font-size:14px;"><b>This goods return cannot be cancelled — the customer has already been credited.</b></p>
  <p>Credit note {0} was raised from this return and is still submitted.</p>
  <p>Cancelling now would send the goods back out to the customer while the credit
     stays on their account — they would keep the goods <b>and</b> the money.</p>
  <p><b>Do this instead:</b> cancel {0} first, then cancel this return.</p>
</div>
"""
		).format(links),
		title=_("Customer already credited"),
	)


def validate_goods_return_comes_from_store(doc, method=None):
	"""Sales Invoice ``validate`` — a credit note for returned goods may only be
	raised by the store, never typed by accounts.

	Goods coming back is a physical event. The person who receives them into the
	store is the only one who knows it happened, so they post the Delivery Note
	Return and the credit note follows automatically — priced at what the
	customer was actually charged, corrections included. Letting accounts raise
	the credit note directly would credit a customer for goods nobody ever
	checked back in, and it is the one remaining way to hand out money without a
	physical event behind it.

	Deliberately *not* blocked:

	  * the return the store's own Delivery Note Return raises
	    (``flags.from_dn_return``);
	  * a price correction, which moves no goods and is identified by
	    ``custom_price_adjustment_for`` (see ``price_adjustment``);
	  * an audited override, ``flags.allow_manual_goods_return``, for the rare
	    case that has to be put right by hand.
	"""
	if not doc.get("is_return"):
		return
	if doc.flags.get("from_dn_return"):
		return
	if doc.get("custom_price_adjustment_for"):
		return
	if doc.flags.get("allow_manual_goods_return"):
		return

	frappe.throw(
		_(
			"""
<div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#222;">
  <p style="font-size:14px;"><b>A credit note for returned goods cannot be raised here.</b></p>
  <p>Goods coming back is something the <b>store</b> records. Accounts cannot
     credit a customer for stock that nobody has checked back in.</p>
  <p><b>What to do instead:</b></p>
  <ol>
    <li>The customer returns the goods to the store.</li>
    <li>The store keeper opens the original <b>Delivery Note</b> and posts a
        <b>Return</b> for the quantity actually received.</li>
    <li>The credit note is then raised <b>automatically</b>, priced at what the
        customer was really charged — including any price correction made since.</li>
  </ol>
  <p>If the price is wrong but no goods are coming back, use
     <b>Corrections &gt; Adjust Price</b> on the invoice instead.</p>
</div>
"""
		),
		title=_("Returns are recorded by the store"),
	)


# ── repair ───────────────────────────────────────────────────────────────────

@frappe.whitelist()
def relink_delivery_note(delivery_note: str, sales_invoice: str) -> dict:
	"""Point an already-submitted Delivery Note at a re-instated Sales Invoice.

	Used to close the repair after a wrongly-cancelled invoice has been amended
	and re-submitted: the note still references the cancelled name, so the pair
	keeps showing up as broken even though the books are right again.

	Only the reference is rewritten — no stock moves, no GL is touched. The new
	invoice must be submitted, must belong to the same customer and company, and
	must be in the note's own amendment chain, so this cannot staple a note onto
	an unrelated sale.
	"""
	dn = frappe.get_doc("Delivery Note", delivery_note)
	si = frappe.get_doc("Sales Invoice", sales_invoice)

	if dn.docstatus != 1:
		frappe.throw(_("Delivery Note {0} is not submitted.").format(delivery_note))
	if si.docstatus != 1:
		frappe.throw(_("Sales Invoice {0} is not submitted.").format(sales_invoice))
	if dn.customer != si.customer or dn.company != si.company:
		frappe.throw(
			_("{0} and {1} are for different customers or companies.").format(
				delivery_note, sales_invoice
			)
		)

	current = {
		row.against_sales_invoice for row in dn.items if row.against_sales_invoice
	}
	chain = set(invoice_amendment_chain(sales_invoice))
	stray = current - chain
	if stray:
		frappe.throw(
			_(
				"{0} references {1}, which is not in the amendment chain of {2}. "
				"Re-linking it would attach the note to an unrelated sale."
			).format(delivery_note, ", ".join(sorted(stray)), sales_invoice)
		)

	# The row-level pointer has to follow the header one. ``si_detail`` names a
	# row of the *cancelled* invoice, and a Delivery Note Return later matches its
	# lines by that id — leave it stale and the return finds nothing to credit and
	# silently raises an empty credit note.
	rows_on_invoice = {
		row.item_code: row.name
		for row in frappe.get_all(
			"Sales Invoice Item",
			filters={"parent": sales_invoice},
			fields=["name", "item_code"],
		)
	}

	updated = 0
	for row in dn.items:
		changes = {}
		if row.against_sales_invoice and row.against_sales_invoice != sales_invoice:
			changes["against_sales_invoice"] = sales_invoice

		new_detail = rows_on_invoice.get(row.item_code)
		if new_detail and row.get("si_detail") != new_detail:
			if not frappe.db.exists(
				"Sales Invoice Item", {"name": row.get("si_detail"), "parent": sales_invoice}
			):
				changes["si_detail"] = new_detail

		for field, value in changes.items():
			frappe.db.set_value(
				"Delivery Note Item", row.name, field, value, update_modified=False
			)
		if changes:
			updated += 1

	return {"delivery_note": delivery_note, "sales_invoice": sales_invoice, "rows_updated": updated}


# ── audit ────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_broken_sales_chains(company: str | None = None) -> list[dict]:
	"""Every submitted forward Delivery Note whose stock is out with no live
	Sales Invoice standing behind it.

	An invoice can be attached to a note in either direction, and both count:

	  * ``Delivery Note Item.against_sales_invoice`` — the invoice-first flow
	    this app automates (SI submitted → draft DN created from it).
	  * ``Sales Invoice Item.delivery_note`` — stock ERPNext's own note-first
	    flow (DN submitted → SI raised from it). Most of the older notes on this
	    site are billed this way.

	Checking only the first direction reports thousands of correctly-billed
	notes as broken, so both are resolved before a note is called orphaned;
	``per_billed`` is honoured as a final corroboration.

	Read-only; safe to run any time. Use it to confirm the books and the store
	agree, and to verify a repair afterwards.
	"""
	filters = {"docstatus": 1, "is_return": 0}
	if company:
		filters["company"] = company

	dns = frappe.get_all(
		"Delivery Note",
		filters=filters,
		fields=["name", "company", "customer", "posting_date", "grand_total", "per_billed"],
	)
	if not dns:
		return []

	dn_names = [d.name for d in dns]
	invoices_of: dict[str, set] = {}

	# Direction 1 — invoice-first (this app's automation).
	for row in frappe.get_all(
		"Delivery Note Item",
		filters={"parent": ["in", dn_names], "against_sales_invoice": ["is", "set"]},
		fields=["parent", "against_sales_invoice"],
	):
		invoices_of.setdefault(row.parent, set()).add(row.against_sales_invoice)

	# Direction 2 — note-first (stock ERPNext).
	for row in frappe.get_all(
		"Sales Invoice Item",
		filters={"delivery_note": ["in", dn_names]},
		fields=["parent", "delivery_note"],
	):
		invoices_of.setdefault(row.delivery_note, set()).add(row.parent)

	all_invoices = sorted({si for names in invoices_of.values() for si in names})
	docstatus_of: dict[str, int] = {}

	# A cancelled invoice that was amended and re-submitted still covers its
	# note — the sale is on the books under the amended name — so amendments
	# are followed forward before anything is called orphaned.
	descendants = invoice_descendants(all_invoices) if all_invoices else {}
	to_check = sorted(
		set(all_invoices) | {child for kids in descendants.values() for child in kids}
	)
	for i in range(0, len(to_check), 500):
		for si in frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", to_check[i:i + 500]]},
			fields=["name", "docstatus"],
		):
			docstatus_of[si.name] = si.docstatus

	def _is_covered(invoice: str) -> bool:
		if docstatus_of.get(invoice) == 1:
			return True
		return any(docstatus_of.get(kid) == 1 for kid in descendants.get(invoice, ()))

	broken = []
	for dn in dns:
		linked = invoices_of.get(dn.name) or set()
		if any(_is_covered(si) for si in linked):
			continue  # a live invoice (or a live amendment of one) stands behind this note
		if not linked and flt(dn.per_billed) >= 100:
			continue  # fully billed through a route we cannot see; not orphaned
		broken.append({
			"delivery_note": dn.name,
			"company": dn.company,
			"customer": dn.customer,
			"posting_date": str(dn.posting_date),
			"grand_total": dn.grand_total,
			"per_billed": dn.per_billed,
			"invoices": sorted(linked),
			"issue": (
				"all linked invoices cancelled" if linked else "no sales invoice linked"
			),
		})
	return sorted(broken, key=lambda r: r["posting_date"])

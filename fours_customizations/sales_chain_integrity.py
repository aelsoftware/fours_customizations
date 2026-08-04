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
from frappe.utils import flt


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
	for i in range(0, len(all_invoices), 500):
		for si in frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", all_invoices[i:i + 500]]},
			fields=["name", "docstatus"],
		):
			docstatus_of[si.name] = si.docstatus

	broken = []
	for dn in dns:
		linked = invoices_of.get(dn.name) or set()
		if any(docstatus_of.get(si) == 1 for si in linked):
			continue  # a live invoice stands behind this note
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

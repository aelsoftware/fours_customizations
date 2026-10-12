"""Private, one-bill credit completion for the management-approved Vox pilot.

This is not a POS credit feature or a payment substitute.  Only corrected sale
4929 / SI-2640-198 can use it.  Native validation, production, stock and accounts
run normally.  The caller owns commit/rollback and MUST roll back any exception.
No API here is whitelisted; no import, preview or verifier writes anything.
"""

import hashlib
import hmac
import json
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal

import frappe
from frappe.utils import get_datetime, get_time, getdate, now_datetime

from fours_customizations import vox_pilot_correction as correction

RECEIPT = "VXN-CREDIT-4929-20260929-v1"
PURPOSE = "vox-exact-pilot-credit-v1"
SUBJECT = "Management-approved credit: KOD-2-101 / sale 4929"
MATERIAL_REQUEST = "MAT-MR-2026-00001"
APPROVAL = "Management approved the exact unused Material Request cancellation and payment on credit."
PROTECTED = (
	"This bill was posted on approved credit. Reload it; use a native Payment Entry to collect the debt."
)
INVALID = (
	"The exact approved credit bill or its server-owned approval changed; management review is required."
)
_ACTIVE = ContextVar("vox_exact_pilot_credit", default=None)
_META = {"modified", "modified_by", "creation", "__islocal", "__unsaved"}
_SOURCE_MUTABLE = _META | {"materialize_state", "_comments", "_assign", "_liked_by", "_user_tags"}
_SCHEDULE_MUTABLE = _META | {
	"name",
	"parent",
	"parentfield",
	"parenttype",
	"docstatus",
	"owner",
	"outstanding",
	"paid_amount",
	"base_outstanding",
	"base_paid_amount",
	"discounted_amount",
}
_INVOICE_HEADERS = (
	"doctype",
	"name",
	"company",
	"customer",
	"debit_to",
	"currency",
	"conversion_rate",
	"pos_profile",
	"set_warehouse",
	"posting_date",
	"posting_time",
	"due_date",
	"set_posting_time",
	"is_pos",
	"is_created_using_pos",
	"update_stock",
	"is_return",
	"return_against",
	"amended_from",
	"is_consolidated",
	"selling_price_list",
	"price_list_currency",
	"plc_conversion_rate",
	"apply_discount_on",
	"additional_discount_percentage",
	"discount_amount",
	"base_discount_amount",
	"total",
	"base_total",
	"net_total",
	"base_net_total",
	"grand_total",
	"base_grand_total",
	"rounded_total",
	"base_rounded_total",
	"rounding_adjustment",
	"base_rounding_adjustment",
	"total_taxes_and_charges",
	"base_total_taxes_and_charges",
	"paid_amount",
	"base_paid_amount",
	"write_off_amount",
	"base_write_off_amount",
	"change_amount",
	"base_change_amount",
	"redeem_loyalty_points",
	"loyalty_amount",
	"is_opening",
	"payment_terms_template",
)


class CreditError(ValueError):
	pass


def _require(condition, message=INVALID):
	if not condition:
		raise CreditError(message)


def _database():
	getter = getattr(frappe.db, "_get_current_object", None)
	return getter() if callable(getter) else frappe.db


@dataclass
class _CreditContext:
	sale: object
	invoice: object
	location: object
	source_digest: str
	user: str
	site: str
	database: object
	receipt: object = None


def _context():
	context = _ACTIVE.get()
	if (
		isinstance(context, _CreditContext)
		and context.user == frappe.session.user == "Administrator"
		and context.site == getattr(frappe.local, "site", None) == correction.SITE
		and context.database is _database()
	):
		return context
	return None


@contextmanager
def _authorizing(sale, invoice, location):
	context = _CreditContext(
		sale, invoice, location, _source_digest(sale), frappe.session.user, frappe.local.site, _database()
	)
	token = _ACTIVE.set(context)
	try:
		yield context
	finally:
		_ACTIVE.reset(token)


def _canonical(value):
	# DB decimal/float fields and Python sums hydrate 0.0 versus 0.  Canonical
	# JSON represents the same finite number identically, without equating it
	# with a quoted client string or changing any persisted business field.
	if isinstance(value, dict):
		_require(all(isinstance(key, str) for key in value))
		return "{" + ",".join(json.dumps(key) + ":" + _canonical(value[key]) for key in sorted(value)) + "}"
	if isinstance(value, list | tuple):
		return "[" + ",".join(_canonical(part) for part in value) + "]"
	if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
		number = correction._number(value)
		return format(number.normalize(), "f") if number else "0"
	return json.dumps(value, default=str)


def _digest(value):
	return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _without_kot(raw):
	value = correction._json(raw)
	value.pop("kot_printed_at", None)
	return value


def _source_values(sale):
	"""Freeze commercial data; native monotonic print facts are not money."""
	value = correction._properties(sale, _SOURCE_MUTABLE | {"lines", "payload"})
	payload = correction._json(sale.get("payload"))
	for name in ("bill_print_count", "receipt_print_count"):
		payload.pop(name, None)
	payload["products"] = [_without_kot(row) for row in payload.get("products") or []]
	value["payload"] = payload
	value["lines"] = []
	for row in sale.get("lines") or []:
		line = correction._properties(row, _META | {"kot_printed_at", "line_payload"})
		line["line_payload"] = _without_kot(row.get("line_payload"))
		value["lines"].append(line)
	# Native Link validation can hydrate an integer name from a canonical text
	# identity.  The identity is still exact, not a numeric-string alias.
	value["name"] = str(correction._id(value.get("name")))
	for row in value["lines"]:
		row["parent"] = str(correction._id(row.get("parent")))
	return value


def _source_digest(sale):
	return _digest(_source_values(sale))


def _invoice_values(invoice):
	value = {field: invoice.get(field) for field in _INVOICE_HEADERS}
	value["posting_date"] = str(getdate(invoice.get("posting_date")))
	value["posting_time"] = str(get_time(invoice.get("posting_time")))
	value["due_date"] = str(getdate(invoice.get("due_date"))) if invoice.get("due_date") else None
	item_fields = set(correction._INVOICE_ITEM_BUSINESS) - {
		correction.PRODUCTION_KEY,
		correction.WORK_ORDER_FIELD,
	}
	value["items"] = [
		{field: row.get(field) for field in sorted(item_fields)} for row in invoice.get("items") or []
	]
	value["payments"] = [correction._properties(row, _META) for row in invoice.get("payments") or []]
	value["advances"] = [correction._properties(row, _META) for row in invoice.get("advances") or []]
	value["taxes"] = [correction._properties(row, _SCHEDULE_MUTABLE) for row in invoice.get("taxes") or []]
	value["payment_schedule"] = [
		correction._properties(row, _SCHEDULE_MUTABLE) for row in invoice.get("payment_schedule") or []
	]
	value["sales_team"] = [
		correction._properties(row, _SCHEDULE_MUTABLE) for row in invoice.get("sales_team") or []
	]
	return value


def _scope(sale, invoice=None):
	if getattr(frappe.local, "site", None) != correction.SITE:
		return False
	return str(sale.get("name")) == correction.SALE or bool(
		invoice and invoice.get("name") == correction.INVOICE
	)


def _exact(sale, invoice):
	_require(
		correction._id(sale.get("name")) == int(correction.SALE)
		and sale.get("sale_uuid") == correction.SALE_UUID
		and sale.get("invoice_no") == correction.LABEL
		and sale.get("sales_invoice") == invoice.get("name") == correction.INVOICE
		and invoice.get("company") == correction.COMPANY
		and invoice.get("doctype") == "Sales Invoice"
		and sale.get("customer") == invoice.get("customer") == "Walk-In Customer"
		and sale.get("location") == invoice.get("pos_profile") == correction.LOCATION
		and correction._id(sale.get("location_id")) == correction.LOCATION_ID
		and sale.get("pos_opening_entry") == correction.OPENING
		and get_datetime(sale.get("transaction_date")) == correction.SOLD_AT
		and getdate(invoice.get("posting_date")) == correction.SOLD_AT.date()
		and get_time(invoice.get("posting_time")) == correction.SOLD_AT.time()
		and not invoice.get("set_warehouse")
		and invoice.get("currency") == "UGX"
		and correction._number(invoice.get("conversion_rate")) == 1
	)
	rows = invoice.get("items") or []
	_require(
		len(rows) == 5
		and rows[0].get("item_code") == correction.NEW_ITEM
		and rows[0].get("warehouse") == correction.KITCHEN
		and all(
			row.get("item_code") == expected[2] and row.get("warehouse") == correction.BAR_WAREHOUSE
			for row, expected in zip(rows[1:], correction.RETAINED, strict=True)
		)
	)


def _credit_contract(sale, invoice):
	_exact(sale, invoice)
	_require(
		sale.get("status") == "final"
		and sale.get("is_suspend") == 0
		and sale.get("payment_status") == "due"
		and not sale.get("payments")
		and correction._number(sale.get("amount_paid")) == 0
		and correction._number(sale.get("payment_revision") or 0) == 0
		and correction._number(sale.get("final_total")) == correction.NEW_TOTAL
		and not any(sale.get(name) for name in ("deleted_at", "merged_into", "voided_bill", "is_quotation"))
	)
	payload = correction._json(sale.get("payload"))
	_require(
		payload.get("is_suspend") == 0
		and not payload.get("payments")
		and correction._number(payload.get("final_total")) == correction.NEW_TOTAL
	)
	_require(
		invoice.get("is_pos") == invoice.get("is_created_using_pos") == 0
		and invoice.get("update_stock") == 1
		and not invoice.get("is_return")
		and not invoice.get("amended_from")
		and not invoice.get("payments")
		and not invoice.get("advances")
		and correction._number(invoice.get("grand_total")) == correction.NEW_TOTAL
		and correction._number(invoice.get("paid_amount")) == 0
		and correction._number(invoice.get("write_off_amount") or 0) == 0
	)


def _signature(body):
	secret = (getattr(frappe, "conf", None) or {}).get("encryption_key")
	_require(
		isinstance(secret, str) and len(secret) >= 32,
		"The private credit approval signing key is unavailable.",
	)
	return hmac.new(secret.encode(), (PURPOSE + "\0" + _canonical(body)).encode(), hashlib.sha256).hexdigest()


def _receipt(*, required=False):
	if not frappe.db.exists("Comment", RECEIPT):
		_require(
			not required,
			"The posted credit bill is missing its protected approval; management review is required.",
		)
		return None
	doc = frappe.get_doc("Comment", RECEIPT)
	_require(
		doc.get("doctype") == "Comment"
		and doc.get("name") == RECEIPT
		and doc.get("comment_type") == "Info"
		and doc.get("subject") == SUBJECT
		and doc.get("reference_doctype") == "Sales Invoice"
		and doc.get("reference_name") == correction.INVOICE
		and doc.get("owner") == doc.get("comment_email") == doc.get("comment_by") == "Administrator"
		and not doc.get("published")
	)
	envelope = correction._json(doc.get("content"))
	_require(
		set(envelope) == {"approval", "signature"}
		and isinstance(envelope["approval"], dict)
		and isinstance(envelope["signature"], str)
	)
	body = envelope["approval"]
	_require(hmac.compare_digest(envelope["signature"], _signature(body)))
	expected = {
		"schema": 1,
		"purpose": PURPOSE,
		"site": correction.SITE,
		"company": correction.COMPANY,
		"sale": correction.SALE,
		"sale_uuid": correction.SALE_UUID,
		"invoice": correction.INVOICE,
		"invoice_no": correction.LABEL,
		"currency": "UGX",
		"credit_total": "54000",
		"new_line_uuid": correction.NEW_LINE_UUID,
		"approved_by": "Administrator",
		"approval": APPROVAL,
		"retired_material_request": MATERIAL_REQUEST,
		"retired_work_order": correction.PLANNING_WORK_ORDER,
	}
	_require(
		all(body.get(key) == value for key, value in expected.items())
		and set(body) == set(expected) | {"approved_at", "source_digest", "invoice_digest", "before_digest"}
	)
	_require(
		all(
			isinstance(body.get(key), str)
			and len(body[key]) == 64
			and all(character in "0123456789abcdef" for character in body[key])
			for key in ("source_digest", "invoice_digest", "before_digest")
		)
	)
	_require(
		isinstance(body.get("approved_at"), str) and get_datetime(body["approved_at"]) >= correction.SOLD_AT
	)
	return body


def _verify_bound(sale, invoice):
	_credit_contract(sale, invoice)
	body = _receipt(required=True)
	_require(
		body["source_digest"] == _source_digest(sale)
		and body["invoice_digest"] == _digest(_invoice_values(invoice))
	)
	return body


def _retired():
	from fours_customizations import vox_work_orders

	request = frappe.get_doc("Material Request", MATERIAL_REQUEST)
	_require(
		request.docstatus == 2
		and request.get("company") == correction.COMPANY
		and request.get("work_order") == correction.PLANNING_WORK_ORDER
	)
	wo = frappe.get_doc("Work Order", correction.PLANNING_WORK_ORDER)
	vox_work_orders._validate_pilot_retirement_work_order(wo, state="cancelled")
	return request


def _production_evidence(sale, invoice):
	from kit_pos.connector import materialize

	from fours_customizations import vox_work_orders

	location = materialize._location_for(sale)
	pairs = vox_work_orders._matched_lines(invoice, sale, location)
	_require(len(pairs) == 1 and pairs[0][1].get("desktop_line_uuid") == correction.NEW_LINE_UUID)
	row, line = pairs[0]
	_require(row.get("item_code") == correction.NEW_ITEM)
	plan = vox_work_orders._plan(invoice, sale, row, line)
	wo = vox_work_orders._existing(plan)
	_require(
		wo is not None
		and row.get(correction.PRODUCTION_KEY)
		== vox_work_orders.production_key(correction.SALE_UUID, correction.NEW_LINE_UUID)
		and row.get(correction.WORK_ORDER_FIELD) == wo.name
	)
	vox_work_orders._assert_lineage(invoice, row, plan, wo)
	vox_work_orders._validate_work_order(wo, plan, completed=True)
	entry = vox_work_orders._manufacture_for(wo, plan)
	stock = frappe.get_all(
		"Stock Ledger Entry",
		filters={
			"voucher_type": "Sales Invoice",
			"voucher_no": correction.INVOICE,
			"item_code": correction.NEW_ITEM,
			"is_cancelled": 0,
		},
		fields=["actual_qty", "warehouse", "voucher_detail_no"],
		limit_page_length=0,
	)
	_require(
		len(stock) == 1
		and correction._number(stock[0].actual_qty) == -1
		and stock[0].warehouse == correction.KITCHEN
		and stock[0].voucher_detail_no == row.name
	)
	ledger = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Sales Invoice", "voucher_no": correction.INVOICE, "is_cancelled": 0},
		fields=["account", "party_type", "party", "debit", "credit"],
		limit_page_length=0,
	)
	receivable = [value for value in ledger if value.account == invoice.get("debit_to")]
	_require(
		ledger
		and receivable
		and all(value.party_type == "Customer" and value.party == invoice.customer for value in receivable)
		and sum(
			(
				correction._number(value.debit or 0) - correction._number(value.credit or 0)
				for value in receivable
			),
			Decimal(0),
		)
		== 54000
		and sum(
			(
				correction._number(value.debit or 0) - correction._number(value.credit or 0)
				for value in ledger
			),
			Decimal(0),
		)
		== 0
	)
	return {"work_order": wo.name, "manufacture": entry.name, "original_receivable": 54000}


def verify_posted_credit(sale, invoice=None):
	"""Read-only connector/closing predicate; never trust a client's marker.

	Later native Payment Entries may reduce outstanding.  Closing may set or
	clear pos_closing_entry.  Neither changes the original credit approval.
	"""
	if not _scope(sale, invoice):
		return False
	invoice = invoice or frappe.get_doc("Sales Invoice", correction.INVOICE)
	receipt_exists = frappe.db.exists("Comment", RECEIPT)
	if not receipt_exists and invoice.get("docstatus") == 0 and invoice.get("is_pos") == 1:
		return False  # The original, not-yet-approved held draft remains normal.
	_verify_bound(sale, invoice)
	_require(
		invoice.get("docstatus") == 1
		and sale.get("materialize_state") == "done"
		and frappe.db.get_value("Sales Invoice", correction.INVOICE, "docstatus") == 1
		and 0 <= correction._number(invoice.get("outstanding_amount")) <= 54000
	)
	_retired()
	_production_evidence(sale, invoice)
	return True


def is_exact_approved_credit(invoice, sale, location):
	"""Initial production needs the private *live invoice object* capability."""
	if not _scope(sale, invoice):
		return False
	_require(
		location
		and location.get("name") == correction.LOCATION
		and location.get("company") == correction.COMPANY
		and location.get("warehouse") == correction.BAR_WAREHOUSE
	)
	_verify_bound(sale, invoice)
	context = _context()
	if context is not None and context.invoice is invoice:
		from kit_pos.connector.materialize import _invoice_submission_context

		native = _invoice_submission_context(invoice)
		_require(
			invoice.get("docstatus") == 1
			and native is not None
			and native.invoice is invoice
			and native.sale is context.sale
			and native.user == context.user
			and sale.get("materialize_state") == "pending"
			and context.source_digest == _source_digest(sale)
			and _source_digest(context.sale) == context.source_digest
			and all(
				context.location.get(key) == location.get(key)
				for key in ("name", "company", "warehouse", "pos_profile", "selling_price_list")
			)
		)
		return True
	return verify_posted_credit(sale, invoice)


def _protected_source():
	if frappe.db.exists("Comment", RECEIPT):
		return True
	invoice = frappe.db.get_value(
		"Sales Invoice", correction.INVOICE, ["docstatus", "is_pos", "is_created_using_pos"], as_dict=True
	)
	return bool(
		invoice and invoice.docstatus == 1 and not invoice.is_pos and not invoice.is_created_using_pos
	)


def guard_credit_source_write(doc, method=None, *args, **kwargs):
	"""Protect both parent REST edits and standalone child price/tender writes."""
	if getattr(frappe.local, "site", None) != correction.SITE:
		return
	child = doc.get("doctype") in {"Kit POS Sale Line", "Kit POS Sale Payment"}
	if child:
		scoped = str(doc.get("parent")) == correction.SALE and doc.get("parenttype") == "Kit POS Sale"
		if not scoped and doc.get("name"):
			saved = frappe.db.get_value(doc.doctype, doc.name, ["parent", "parenttype"], as_dict=True)
			scoped = bool(
				saved and str(saved.parent) == correction.SALE and saved.parenttype == "Kit POS Sale"
			)
		if not scoped:
			return
	elif doc.get("doctype") != "Kit POS Sale" or str(doc.get("name")) != correction.SALE:
		return
	context = _context()
	if context is not None and method not in {"on_trash", "before_rename"}:
		objects = [context.sale, *(context.sale.get("lines") or [])]
		if any(doc is value for value in objects):
			_require(_source_digest(context.sale) == context.source_digest, PROTECTED)
			return
	if not _protected_source():
		return
	_receipt(required=True)
	_require(not child and method not in {"on_trash", "before_rename"}, PROTECTED)
	invoice = frappe.get_doc("Sales Invoice", correction.INVOICE)
	_require(verify_posted_credit(doc, invoice), PROTECTED)


def guard_credit_approval(doc, method=None, *args, **kwargs):
	# Frappe calls before_rename(old, new, merge), then strips the destination
	# in validate_name.  Reserve this logical Comment key before it exists too;
	# an ordinary Comment must never be renamed/merged into the approval slot.
	destination = (args[1] if len(args) > 1 else kwargs.get("new")) if method == "before_rename" else None
	reserved_destination = (
		isinstance(destination, str) and destination.strip().casefold() == RECEIPT.casefold()
	)
	if (
		getattr(frappe.local, "site", None) != correction.SITE
		or doc.get("doctype") != "Comment"
		or (doc.get("name") != RECEIPT and not reserved_destination)
	):
		return
	context = _context()
	_require(
		context is not None
		and context.receipt is doc
		and method not in {"on_trash", "before_rename"}
		and not frappe.db.exists("Comment", RECEIPT),
		"This management credit approval is immutable.",
	)


def guard_credit_invoice(doc, method=None, *args, **kwargs):
	"""Keep this approved native credit bill aligned with its protected source.

	A future cancellation/amendment needs a separately reviewed, coordinated
	correction path; normal invoice cancellation cannot orphan the credit sale.
	Native collections and closing references are non-commercial changes and
	remain permitted by the signed invoice projection.
	"""
	if getattr(frappe.local, "site", None) != correction.SITE or doc.get("doctype") != "Sales Invoice":
		return
	destination = (args[1] if len(args) > 1 else kwargs.get("new")) if method == "before_rename" else None
	reserved_destination = (
		isinstance(destination, str) and destination.strip().casefold() == correction.INVOICE.casefold()
	)
	if doc.get("name") != correction.INVOICE and not reserved_destination:
		return
	if not _protected_source():
		return  # The original held draft has not acquired any credit approval.
	_require(
		method not in {"before_cancel", "on_trash", "before_rename"},
		"This approved credit invoice requires a reviewed correction before cancellation, deletion or renaming.",
	)
	sale = frappe.get_doc("Kit POS Sale", correction.SALE)
	context = _context()
	if context is not None and context.invoice is doc:
		# Some native submit hooks may save allowed bookkeeping fields before
		# the helper marks the source done.  Only the real submitting object,
		# never a flag or a copied invoice, has this temporary capability.
		_require(is_exact_approved_credit(doc, sale, context.location), PROTECTED)
		return
	_require(verify_posted_credit(sale, doc), PROTECTED)


def _read(*, locked=False):
	from kit_pos.connector import materialize

	sale = (
		materialize._locked_sale(correction.SALE)
		if locked
		else frappe.get_doc("Kit POS Sale", correction.SALE)
	)
	invoice = frappe.get_doc("Sales Invoice", correction.INVOICE, for_update=locked)
	if invoice.docstatus == 1 or frappe.db.exists("Comment", RECEIPT):
		_require(verify_posted_credit(sale, invoice))
		return {
			"sale": sale,
			"invoice": invoice,
			"receipt": _receipt(required=True),
			"state": "already_posted",
		}
	if locked:
		materialize._reject_finalized_opening(sale)
	current = correction._read_state(locked=locked)
	_require(
		correction._validate_state(current) == "already_corrected",
		"First complete the exact approved held-bill correction; credit completion does not replace items.",
	)
	current["request"] = _retired()
	current.update(receipt=None, state="ready_credit")
	return current


def _review_digest(state):
	return _digest(
		{key: correction._dict(value) if hasattr(value, "as_dict") else value for key, value in state.items()}
	)


def _result(state):
	return {
		"state": state["state"],
		"sale": correction.SALE,
		"invoice": correction.INVOICE,
		"invoice_no": correction.LABEL,
		"before_digest": _review_digest(state),
		"credit_total": 54000,
		"paid_amount": 0,
		"outstanding_amount": state["invoice"].get("outstanding_amount"),
		"held": bool(state["sale"].get("is_suspend")),
		"submitted": state["invoice"].docstatus == 1,
		"approval_comment": RECEIPT if state["receipt"] else None,
		"transaction_owner": "caller",
	}


def _access(sale, payload):
	from kit_pos.connector import sale_permissions, sales

	access = sale_permissions.PosAccess()
	access.require("sell.update", "completing this exact approved credit bill")
	access.require_sale(sale, correction.SALE)
	access.require_location(correction.LOCATION_ID)
	sale_permissions.validate_saved_creator(sale, payload)
	sales._authorize_sale_payload(access, payload, sale=sale)
	_require(access.user == "Administrator")
	return access


def preview_credit():
	correction._guard()
	state = _read()
	payload = correction._json(state["sale"].get("payload"))
	payload["is_suspend"] = 0
	_access(state["sale"], payload)
	return _result(state)


def _insert_receipt(context, before_digest):
	body = {
		"schema": 1,
		"purpose": PURPOSE,
		"site": correction.SITE,
		"company": correction.COMPANY,
		"sale": correction.SALE,
		"sale_uuid": correction.SALE_UUID,
		"invoice": correction.INVOICE,
		"invoice_no": correction.LABEL,
		"currency": "UGX",
		"credit_total": "54000",
		"new_line_uuid": correction.NEW_LINE_UUID,
		"approved_by": "Administrator",
		"approval": APPROVAL,
		"retired_material_request": MATERIAL_REQUEST,
		"retired_work_order": correction.PLANNING_WORK_ORDER,
		"approved_at": str(now_datetime()),
		"source_digest": context.source_digest,
		"invoice_digest": _digest(_invoice_values(context.invoice)),
		"before_digest": before_digest,
	}
	doc = frappe.get_doc(
		{
			"doctype": "Comment",
			"name": RECEIPT,
			"comment_type": "Info",
			"subject": SUBJECT,
			"reference_doctype": "Sales Invoice",
			"reference_name": correction.INVOICE,
			"comment_email": "Administrator",
			"comment_by": "Administrator",
			"published": 0,
			"content": _canonical({"approval": body, "signature": _signature(body)}),
		}
	)
	context.receipt = doc
	doc.insert(set_name=RECEIPT)
	_require(_receipt(required=True) == body)


def apply_credit(expected_digest):
	"""Atomic native credit completion; the caller must roll back ANY error."""
	from kit_pos.connector import materialize, sales

	correction._guard()
	with correction._no_commit_or_queue():
		state = _read(locked=True)
		digest = _review_digest(state)
		if state["state"] == "already_posted":
			_require(
				expected_digest in {digest, state["receipt"]["before_digest"]},
				"Credit preview changed; review a fresh digest.",
			)
			return _result(state)
		_require(expected_digest == digest, "Credit preview changed; review a fresh digest.")
		sale, invoice, location = state["sale"], state["invoice"], state["location"]
		payload = correction._json(sale.get("payload"))
		payload["is_suspend"] = 0
		_access(sale, payload)
		invoice.check_permission("submit")
		due_date = invoice.get("due_date")
		old_lines = deepcopy(sale.get("lines"))
		sale.is_suspend, sale.materialize_state = 0, "pending"
		sale.payload = json.dumps(payload, default=str)
		sales.settle_money(sale)
		with _authorizing(sale, invoice, location) as context:
			sale.save()
			invoice.is_pos = invoice.is_created_using_pos = 0
			if not invoice.get("sales_team"):
				for row in materialize._sales_team(sale):
					invoice.append("sales_team", row)
			invoice.save()
			_require(
				invoice.get("due_date") == due_date,
				"Native credit validation changed the due date; review before posting.",
			)
			_credit_contract(sale, invoice)
			_require(_source_digest(sale) == context.source_digest)
			_insert_receipt(context, expected_digest)
			with materialize._submitting_invoice(invoice, sale, location):
				invoice.submit()
			sale.materialize_state = "done"
			sale.save()
			_require(
				_source_digest(sale) == context.source_digest
				and all(
					correction._properties(old, _META) == correction._properties(new, _META)
					for old, new in zip(old_lines, sale.get("lines"), strict=True)
				)
			)
		current = _read()
		_require(
			current["state"] == "already_posted"
			and correction._number(current["invoice"].get("outstanding_amount")) == 54000
		)
		_require(
			not frappe.db.count(
				"Payment Entry Reference",
				{"reference_doctype": "Sales Invoice", "reference_name": correction.INVOICE},
			)
			and not frappe.db.count(
				"Journal Entry Account",
				{"reference_type": "Sales Invoice", "reference_name": correction.INVOICE},
			)
		)
		result = _result(current)
		result.update(
			state="posted_credit",
			previous_digest=expected_digest,
			**_production_evidence(current["sale"], current["invoice"]),
		)
		return result

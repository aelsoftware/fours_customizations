"""Controlled, one-time accounting corrections for submitted 4S documents.

The public API deliberately accepts only a document identity, an enumerated
action, and its tightly scoped replacement values. Allocation amounts are
accepted only for redistributing an already-paid invoice total; the total is
immutable. The submitted source keeps its name and docstatus while ERPNext's
repost machinery replaces the affected ledger rows in one transaction.
"""

from __future__ import annotations

import copy
import hashlib
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime

FOUR_S_COMPANY = "4S Industries Limited"
ACCOUNTANT_ROLE = "4S Accountant"
AUDIT_DOCTYPE = "Four S Accounting Correction"
MIN_REASON_LENGTH = 10

CHANGE_POSTING_DATE = "change_posting_date"
EXCLUDE_PAYMENT = "exclude_payment"
INCLUDE_PAYMENT = "include_payment"
CHANGE_MODE_OF_PAYMENT = "change_mode_of_payment"

ACTION_LABELS = {
	CHANGE_POSTING_DATE: "Change Posting Date",
	EXCLUDE_PAYMENT: "Exclude Invoice Payment",
	INCLUDE_PAYMENT: "Include Invoice Payment",
	CHANGE_MODE_OF_PAYMENT: "Change Mode of Payment",
}

DOCTYPE_ACTIONS = {
	"Sales Invoice": {
		CHANGE_POSTING_DATE,
		EXCLUDE_PAYMENT,
		INCLUDE_PAYMENT,
		CHANGE_MODE_OF_PAYMENT,
	},
	"Payment Entry": {CHANGE_POSTING_DATE, CHANGE_MODE_OF_PAYMENT},
}

SALES_TOTAL_FIELDS = (
	"total_qty",
	"base_total",
	"base_net_total",
	"total",
	"net_total",
	"base_total_taxes_and_charges",
	"total_taxes_and_charges",
	"base_grand_total",
	"grand_total",
	"base_rounded_total",
	"rounded_total",
	"base_discount_amount",
	"discount_amount",
	"write_off_amount",
	"base_write_off_amount",
	"loyalty_amount",
)

SALES_PAYMENT_TOTAL_FIELDS = (
	"paid_amount",
	"base_paid_amount",
	"change_amount",
	"base_change_amount",
	"outstanding_amount",
)

PAYMENT_ENTRY_AMOUNT_FIELDS = (
	"paid_amount",
	"received_amount",
	"base_paid_amount",
	"base_received_amount",
	"total_allocated_amount",
	"unallocated_amount",
	"difference_amount",
	"source_exchange_rate",
	"target_exchange_rate",
)

ADVANCE_RECONCILIATION_REFERENCE_DOCTYPES = {
	"Sales Invoice",
	"Purchase Invoice",
	"Journal Entry",
	"Payment Entry",
}

GL_AMOUNT_FIELDS = {
	"debit",
	"credit",
	"debit_in_account_currency",
	"credit_in_account_currency",
	"debit_in_transaction_currency",
	"credit_in_transaction_currency",
	"debit_in_reporting_currency",
	"credit_in_reporting_currency",
}

GL_NUMERIC_FIELDS = GL_AMOUNT_FIELDS | {
	"transaction_exchange_rate",
	"reporting_currency_exchange_rate",
}

PAYMENT_SCHEDULE_FIELDS = (
	"idx",
	"payment_term",
	"description",
	"due_date",
	"invoice_portion",
	"payment_amount",
	"base_payment_amount",
	"mode_of_payment",
	"paid_amount",
	"base_paid_amount",
	"discounted_amount",
	"outstanding",
	"base_outstanding",
	"discount_date",
	"discount_type",
	"discount",
	"due_date_based_on",
	"credit_days",
	"credit_months",
	"discount_validity_based_on",
	"discount_validity",
)

PAYMENT_SCHEDULE_NUMERIC_FIELDS = {
	"invoice_portion",
	"payment_amount",
	"base_payment_amount",
	"paid_amount",
	"base_paid_amount",
	"discounted_amount",
	"outstanding",
	"base_outstanding",
	"discount",
	"credit_days",
	"credit_months",
	"discount_validity",
}

GL_SNAPSHOT_FIELDS = (
	"docstatus",
	"posting_date",
	"transaction_date",
	"fiscal_year",
	"company",
	"account",
	"against",
	"party_type",
	"party",
	"debit",
	"credit",
	"debit_in_account_currency",
	"credit_in_account_currency",
	"debit_in_transaction_currency",
	"credit_in_transaction_currency",
	"account_currency",
	"transaction_currency",
	"transaction_exchange_rate",
	"cost_center",
	"project",
	"finance_book",
	"voucher_type",
	"voucher_no",
	"voucher_subtype",
	"voucher_detail_no",
	"against_voucher_type",
	"against_voucher",
	"due_date",
	"is_opening",
	"is_advance",
	"remarks",
	"reporting_currency_exchange_rate",
	"debit_in_reporting_currency",
	"credit_in_reporting_currency",
)

PAYMENT_LEDGER_SNAPSHOT_FIELDS = (
	"docstatus",
	"posting_date",
	"company",
	"account",
	"account_type",
	"party_type",
	"party",
	"due_date",
	"amount",
	"amount_in_account_currency",
	"account_currency",
	"cost_center",
	"project",
	"finance_book",
	"voucher_type",
	"voucher_no",
	"voucher_detail_no",
	"against_voucher_type",
	"against_voucher_no",
	"remarks",
)

ADVANCE_PAYMENT_LEDGER_SNAPSHOT_FIELDS = (
	"docstatus",
	"company",
	"voucher_type",
	"voucher_no",
	"against_voucher_type",
	"against_voucher_no",
	"amount",
	"base_amount",
	"currency",
	"exchange_rate",
	"event",
)

PAYMENT_LEDGER_NUMERIC_FIELDS = {"amount", "amount_in_account_currency"}
ADVANCE_PAYMENT_LEDGER_NUMERIC_FIELDS = {"amount", "base_amount", "exchange_rate"}


@frappe.whitelist()
def get_correction_context(doctype: str, name: str) -> dict:
	"""Return available one-time actions and safe dialog choices for a document."""
	doc = _get_authorized_document(doctype, name)
	used = _used_actions(doc)
	payment_rows = _invoice_payment_rows(doc) if doctype == "Sales Invoice" else []

	stock_backed_invoice = doctype == "Sales Invoice" and cint(doc.get("update_stock"))
	available = {
		CHANGE_POSTING_DATE: not stock_backed_invoice and CHANGE_POSTING_DATE not in used,
		CHANGE_MODE_OF_PAYMENT: CHANGE_MODE_OF_PAYMENT not in used,
	}
	if doctype == "Sales Invoice":
		include_amount = _invoice_include_amount(doc)
		has_payment_references = any(
			(row.get("reference_no") or "").strip() for row in (doc.get("payments") or [])
		)
		available.update(
			{
				EXCLUDE_PAYMENT: bool(cint(doc.is_pos)) and EXCLUDE_PAYMENT not in used,
				INCLUDE_PAYMENT: bool(not cint(doc.is_pos) and include_amount)
				and INCLUDE_PAYMENT not in used,
				CHANGE_MODE_OF_PAYMENT: bool(
					cint(doc.is_pos) and payment_rows and not has_payment_references
				)
				and CHANGE_MODE_OF_PAYMENT not in used,
			}
		)
	else:
		include_amount = None

	if doc.doctype == "Payment Entry":
		if doc.payment_type == "Receive":
			payment_account_currency = doc.get("paid_to_account_currency")
		elif doc.payment_type == "Pay":
			payment_account_currency = doc.get("paid_from_account_currency")
		else:
			payment_account_currency = None
	else:
		payment_account_currency = doc.get("currency")

	return {
		"doctype": doc.doctype,
		"name": doc.name,
		"company": doc.company,
		"posting_date": str(doc.posting_date),
		"modified": str(doc.modified),
		"is_pos": cint(doc.get("is_pos")),
		"update_stock": cint(doc.get("update_stock")),
		"mode_of_payment": doc.get("mode_of_payment"),
		"payment_type": doc.get("payment_type"),
		"paid_from": doc.get("paid_from"),
		"paid_to": doc.get("paid_to"),
		"reference_no": doc.get("reference_no"),
		"reference_date": str(doc.get("reference_date") or ""),
		"payment_rows": payment_rows,
		"include_amount": include_amount,
		"currency": payment_account_currency,
		"modes_of_payment": _mode_of_payment_choices(doc.company),
		"available_actions": available,
		"used_actions": used,
	}


@frappe.whitelist(methods=["POST"])
def apply_correction(
	doctype: str,
	name: str,
	action: str,
	reason: str,
	expected_modified: str | None = None,
	new_posting_date: str | None = None,
	new_mode_of_payment: str | None = None,
	payment_row: str | None = None,
	payment_allocations: str | list[dict] | None = None,
	account_side: str | None = None,
	new_reference_no: str | None = None,
	new_reference_date: str | None = None,
) -> dict:
	"""Apply one tightly-scoped correction and repost every affected ledger."""
	if doctype not in DOCTYPE_ACTIONS or action not in DOCTYPE_ACTIONS[doctype]:
		frappe.throw(_("This accounting correction is not supported."))

	reason = (reason or "").strip()
	if len(reason) < MIN_REASON_LENGTH:
		frappe.throw(
			_("Correction reason must contain at least {0} characters.").format(MIN_REASON_LENGTH)
		)

	# Row locking makes the one-use rule deterministic even if two browser tabs
	# submit the same action at the same time.
	doc = _get_authorized_document(doctype, name, for_update=True)
	if not expected_modified or get_datetime(expected_modified) != get_datetime(doc.modified):
		frappe.throw(
			_("This document changed after the correction window was opened. Reload it and review the latest values before trying again."),
			title=_("Document Changed"),
		)
	key = _action_key(doc.doctype, doc.name, action)
	if frappe.db.exists(AUDIT_DOCTYPE, {"unique_action_key": key}):
		frappe.throw(
			_("{0} has already been used once on {1}.").format(ACTION_LABELS[action], doc.name),
			title=_("Correction Already Used"),
		)
	if doc.doctype == "Payment Entry":
		_validate_payment_entry_correction_dependencies(doc)
	else:
		_validate_sales_invoice_correction_dependencies(doc, action)
	_lock_active_ledger_rows(doc)

	old_doc = copy.deepcopy(doc)
	old_values = _document_state(old_doc)
	ledger_before = _ledger_snapshot(old_doc)
	protected_before = _protected_amounts(old_doc)

	with _correction_write_context():
		change_details = _apply_requested_change(
			doc,
			action,
			new_posting_date=new_posting_date,
			new_mode_of_payment=new_mode_of_payment,
			payment_row=payment_row,
			payment_allocations=payment_allocations,
			account_side=account_side,
			new_reference_no=new_reference_no,
			new_reference_date=new_reference_date,
		)
		_validate_protected_amounts(doc, protected_before, action)
		_validate_protected_document(old_doc, doc, action, change_details)
		_save_submitted_document(doc)
		_validate_protected_amounts(doc, protected_before, action)
		_validate_protected_document(old_doc, doc, action, change_details)

		repost_reference = _repost_accounting_ledger(doc)
		doc.reload()
		_validate_protected_amounts(doc, protected_before, action)
		_validate_protected_document(old_doc, doc, action, change_details)
		ledger_after = _ledger_snapshot(doc)
		_verify_result(doc, action, change_details, ledger_before, ledger_after)

		log = _create_audit_log(
			doc=doc,
			action=action,
			reason=reason,
			key=key,
			old_values=old_values,
			new_values=_document_state(doc),
			ledger_before=ledger_before,
			ledger_after=ledger_after,
			repost_reference=repost_reference,
		)
		_add_timeline_comment(doc, log, action, reason, old_values, _document_state(doc))

	message = _("{0} was applied to {1}.").format(ACTION_LABELS[action], doc.name)
	return {
		"name": doc.name,
		"doctype": doc.doctype,
		"action": action,
		"action_label": ACTION_LABELS[action],
		"audit_log": log.name,
		"message": message,
	}


def _get_authorized_document(doctype: str, name: str, for_update: bool = False):
	if doctype not in DOCTYPE_ACTIONS:
		frappe.throw(_("Only Sales Invoice and Payment Entry can be corrected here."))
	if ACCOUNTANT_ROLE not in frappe.get_roles(frappe.session.user):
		frappe.throw(
			_("Only users with the {0} role can use accounting corrections.").format(
				frappe.bold(ACCOUNTANT_ROLE)
			),
			frappe.PermissionError,
		)

	doc = frappe.get_doc(doctype, name, for_update=for_update)
	doc.check_permission("read")
	if doc.company != FOUR_S_COMPANY:
		frappe.throw(
			_("Accounting corrections are restricted to {0}.").format(frappe.bold(FOUR_S_COMPANY)),
			frappe.PermissionError,
		)
	if doc.docstatus != 1:
		frappe.throw(_("Only submitted documents can be corrected."))
	return doc


def _used_actions(doc) -> dict:
	reverse_labels = {label: code for code, label in ACTION_LABELS.items()}
	rows = frappe.db.get_all(
		AUDIT_DOCTYPE,
		filters={"reference_doctype": doc.doctype, "reference_name": doc.name},
		fields=["name", "action", "performed_by", "performed_on"],
		order_by="performed_on asc",
	)
	return {
		reverse_labels[row.action]: {
			"name": row.name,
			"action": row.action,
			"performed_by": row.performed_by,
			"performed_on": row.performed_on,
		}
		for row in rows
		if row.action in reverse_labels
	}


def _action_key(doctype: str, name: str, action: str) -> str:
	return hashlib.sha256(f"{doctype}\0{name}\0{action}".encode()).hexdigest()


def _validate_payment_entry_correction_dependencies(doc):
	"""Fail closed before a Payment Entry or any dependent ledger is changed.

	Core's Payment Entry repost path cancels linked exchange gain/loss journals
	and may recreate only the journals driven by this entry's current reference
	rows.  A reconciled payment also carries bank-statement state that cannot be
	safely moved to a different account or date here.  Immutable-ledger reposts
	produce reversal rows whose correction semantics need a separate workflow.
	"""
	_validate_immutable_ledger_is_disabled()
	_validate_open_accounting_dates(doc, doc.posting_date, doc.posting_date)
	_validate_payment_entry_not_reconciled(doc)
	_validate_no_linked_exchange_gain_loss_journals(doc)


def _validate_sales_invoice_correction_dependencies(doc, action: str):
	if action == CHANGE_POSTING_DATE and cint(doc.get("update_stock")):
		frappe.throw(
			_(
				"Posting Date cannot be changed here for a stock-backed Sales Invoice because stock valuation reposting is asynchronous."
			),
			title=_("Stock-backed Date Change Not Supported"),
		)
	_validate_immutable_ledger_is_disabled()
	_validate_open_accounting_dates(doc, doc.posting_date, doc.posting_date)
	_validate_sales_invoice_not_reconciled(doc)
	_validate_sales_invoice_operational_dependencies(doc, action)


def _validate_sales_invoice_operational_dependencies(doc, action: str):
	"""Reject source documents whose dependent records are outside this workflow.

	Those dependencies are normally maintained by Sales Invoice submit/cancel
	hooks.  A targeted ledger repost deliberately does not run those hooks, so a
	correction must not leave the dependent record disagreeing with the invoice.
	"""
	if cint(doc.get("is_consolidated")):
		frappe.throw(
			_("Consolidated Sales Invoices must be corrected through their POS Closing Entry."),
			title=_("POS Closing Dependency"),
		)

	if cint(doc.get("is_created_using_pos")) or doc.get("pos_closing_entry"):
		frappe.throw(
			_(
				"Sales Invoice {0} is linked to the POS closing workflow and cannot be changed by this correction."
			).format(frappe.bold(doc.name)),
			title=_("POS Closing Dependency"),
		)

	include_exclude = action in {INCLUDE_PAYMENT, EXCLUDE_PAYMENT}
	if include_exclude and (
		flt(doc.get("write_off_amount")) or flt(doc.get("base_write_off_amount"))
	):
		frappe.throw(
			_("Include/Exclude Payment is not supported on an invoice with a write-off."),
			title=_("Write-off Dependency"),
		)

	dependent_actions = {CHANGE_POSTING_DATE, INCLUDE_PAYMENT, EXCLUDE_PAYMENT}
	if action not in dependent_actions:
		return

	if (
		cint(doc.get("redeem_loyalty_points"))
		or flt(doc.get("loyalty_points"))
		or flt(doc.get("loyalty_amount"))
	):
		frappe.throw(
			_("This invoice has loyalty-ledger dependencies and cannot use this correction."),
			title=_("Loyalty Dependency"),
		)

	if any(
		row.get("asset") or cint(row.get("is_fixed_asset"))
		for row in (doc.get("items") or [])
	):
		frappe.throw(
			_("A fixed-asset Sales Invoice cannot use this correction."),
			title=_("Asset Dependency"),
		)

	linked_journals = frappe.db.sql(
		"""
		SELECT DISTINCT je.name
		FROM `tabJournal Entry Account` jea
		INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
		WHERE jea.reference_type = 'Sales Invoice'
			AND jea.reference_name = %(sales_invoice)s
			AND jea.docstatus = 1
			AND je.docstatus = 1
			AND je.is_system_generated = 1
		FOR UPDATE
		""",
		{"sales_invoice": doc.name},
		as_dict=True,
	)
	if linked_journals:
		frappe.throw(
			_(
				"Sales Invoice {0} is linked to submitted system Journal Entry {1}. "
				"Correct that dependency through a reviewed accounting adjustment first."
			).format(
				frappe.bold(doc.name),
				frappe.bold(", ".join(sorted(row.name for row in linked_journals))),
			),
			title=_("Linked Accounting Dependency"),
		)


def _validate_immutable_ledger_is_disabled():
	if cint(frappe.get_single_value("Accounts Settings", "enable_immutable_ledger")):
		frappe.throw(
			_(
				"Accounting corrections are disabled while Immutable Ledger is enabled. "
				"Use a reversing accounting document instead."
			),
			title=_("Immutable Ledger Enabled"),
		)


def _validate_sales_invoice_not_reconciled(doc):
	cleared_rows = [row.idx for row in (doc.get("payments") or []) if row.get("clearance_date")]
	if cleared_rows:
		frappe.throw(
			_(
				"Sales Invoice {0} has cleared payment row(s) {1}. Remove the bank reconciliation "
				"before applying an accounting correction."
			).format(frappe.bold(doc.name), frappe.bold(", ".join(map(str, cleared_rows)))),
			title=_("Bank Reconciliation Exists"),
		)

	allocations = frappe.db.sql(
		"""
		SELECT btp.name, btp.parent
		FROM `tabBank Transaction Payments` btp
		INNER JOIN `tabBank Transaction` bt ON bt.name = btp.parent
		WHERE btp.payment_document = 'Sales Invoice'
			AND btp.payment_entry = %(sales_invoice)s
			AND bt.docstatus < 2
		FOR UPDATE
		""",
		{"sales_invoice": doc.name},
		as_dict=True,
	)
	if allocations:
		transactions = sorted({row.parent for row in allocations})
		frappe.throw(
			_(
				"Sales Invoice {0} is allocated to Bank Transaction {1}. "
				"Unreconcile it before applying an accounting correction."
			).format(frappe.bold(doc.name), frappe.bold(", ".join(transactions))),
			title=_("Bank Reconciliation Exists"),
		)


def _validate_payment_entry_not_reconciled(doc):
	if doc.get("clearance_date"):
		frappe.throw(
			_(
				"Payment Entry {0} has a Clearance Date. Remove its bank reconciliation "
				"before applying an accounting correction."
			).format(frappe.bold(doc.name)),
			title=_("Bank Reconciliation Exists"),
		)

	# Lock matching allocation rows so a bank reconciliation cannot be changed
	# between this guard and the before-ledger snapshot in the same transaction.
	allocations = frappe.db.sql(
		"""
		SELECT btp.name, btp.parent
		FROM `tabBank Transaction Payments` btp
		INNER JOIN `tabBank Transaction` bt ON bt.name = btp.parent
		WHERE btp.payment_document = 'Payment Entry'
			AND btp.payment_entry = %(payment_entry)s
			AND bt.docstatus < 2
		FOR UPDATE
		""",
		{"payment_entry": doc.name},
		as_dict=True,
	)
	if allocations:
		transactions = sorted({row.parent for row in allocations})
		frappe.throw(
			_(
				"Payment Entry {0} is allocated to Bank Transaction {1}. "
				"Unreconcile it before applying an accounting correction."
			).format(frappe.bold(doc.name), frappe.bold(", ".join(transactions))),
			title=_("Bank Reconciliation Exists"),
		)


def _validate_no_linked_exchange_gain_loss_journals(doc):
	# Repost Accounting Ledger invokes PaymentEntry.make_gl_entries(cancel=1),
	# which cancels these journals as a side effect.  Some journals reference two
	# Payment Entries and cannot be safely regenerated from either entry alone.
	rows = frappe.db.sql(
		"""
		SELECT je.name
		FROM `tabJournal Entry Account` jea
		INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
		WHERE jea.reference_type = 'Payment Entry'
			AND jea.reference_name = %(payment_entry)s
			AND jea.docstatus = 1
			AND je.docstatus = 1
			AND je.voucher_type = 'Exchange Gain Or Loss'
			AND je.is_system_generated = 1
		FOR UPDATE
		""",
		{"payment_entry": doc.name},
		as_dict=True,
	)
	if rows:
		journals = sorted({row.name for row in rows})
		frappe.throw(
			_(
				"Payment Entry {0} is linked to submitted Exchange Gain/Loss Journal Entry {1}. "
				"Correct that dependency through a reviewed accounting adjustment first."
			).format(frappe.bold(doc.name), frappe.bold(", ".join(journals))),
			title=_("Linked Exchange Gain/Loss Entry"),
		)


def _lock_active_ledger_rows(doc):
	"""Hold voucher-ledger rows stable until verification and audit are complete."""
	queries = (
		("GL Entry", "is_cancelled = 0"),
		("Payment Ledger Entry", "delinked = 0"),
		("Advance Payment Ledger Entry", "delinked = 0"),
	)
	if doc.doctype == "Sales Invoice" and cint(doc.get("update_stock")):
		queries += (("Stock Ledger Entry", "is_cancelled = 0"),)

	for table, active_filter in queries:
		frappe.db.sql(
			f"""
			SELECT name
			FROM `tab{table}`
			WHERE voucher_type = %(voucher_type)s
				AND voucher_no = %(voucher_no)s
				AND {active_filter}
			FOR UPDATE
			""",
			{"voucher_type": doc.doctype, "voucher_no": doc.name},
		)


@contextmanager
def _correction_write_context():
	marker = "in_four_s_accounting_correction"
	previous = frappe.flags.get(marker)
	frappe.flags[marker] = True
	try:
		yield
	finally:
		if previous is None:
			frappe.flags.pop(marker, None)
		else:
			frappe.flags[marker] = previous


def _apply_requested_change(doc, action: str, **values) -> dict:
	if action == CHANGE_POSTING_DATE:
		return _change_posting_date(doc, values.get("new_posting_date"))
	if action == EXCLUDE_PAYMENT:
		return _exclude_invoice_payment(doc)
	if action == INCLUDE_PAYMENT:
		return _include_invoice_payment(doc, values.get("new_mode_of_payment"))
	if action == CHANGE_MODE_OF_PAYMENT:
		if doc.doctype == "Sales Invoice":
			return _change_invoice_mode_of_payment(
				doc,
				values.get("payment_row"),
				values.get("new_mode_of_payment"),
				values.get("payment_allocations"),
			)
		return _change_payment_entry_mode_of_payment(
			doc,
			values.get("new_mode_of_payment"),
			values.get("account_side"),
			values.get("new_reference_no"),
			values.get("new_reference_date"),
		)
	frappe.throw(_("Unsupported accounting correction."))


def _change_posting_date(doc, new_posting_date: str | None) -> dict:
	if doc.doctype == "Sales Invoice" and cint(doc.get("update_stock")):
		frappe.throw(
			_(
				"Posting Date cannot be changed here for a stock-backed Sales Invoice because stock valuation reposting is asynchronous."
			),
			title=_("Stock-backed Date Change Not Supported"),
		)
	if not new_posting_date:
		frappe.throw(_("New Posting Date is required."))
	new_date = getdate(new_posting_date)
	old_date = getdate(doc.posting_date)
	if new_date == old_date:
		frappe.throw(_("Choose a different Posting Date."))
	_validate_open_accounting_dates(doc, old_date, new_date)
	doc.posting_date = new_date
	if doc.doctype == "Sales Invoice":
		# Otherwise TransactionBase.validate_posting_time silently restores today's
		# date during the submitted-document save.
		doc.set_posting_time = 1
	return {"old_posting_date": old_date, "new_posting_date": new_date}


def _validate_open_accounting_dates(doc, old_date, new_date):
	"""Prevent a correction from either removing entries from, or adding them to,
	a frozen/closed accounting period.
	"""
	from erpnext.accounts.general_ledger import (
		check_freezing_date,
		validate_accounting_period,
		validate_against_pcv,
	)

	is_opening = doc.get("is_opening") == "Yes"
	for posting_date in {getdate(old_date), getdate(new_date)}:
		_validate_correction_date_is_open(doc, posting_date, is_opening)
		check_freezing_date(posting_date, doc.company)
		validate_against_pcv(is_opening, posting_date, doc.company)
		validate_accounting_period(
			[
				frappe._dict(
					posting_date=posting_date,
					company=doc.company,
					voucher_type=doc.doctype,
				)
			]
		)


def _validate_correction_date_is_open(doc, posting_date, is_opening: bool):
	"""Enforce closed periods without honoring operational role exemptions."""
	posting_date = getdate(posting_date)
	frozen_till = frappe.db.get_value("Company", doc.company, "accounts_frozen_till_date")
	if frozen_till and posting_date <= getdate(frozen_till):
		frappe.throw(
			_("Accounting corrections cannot use a date on or before {0}.").format(
				frappe.bold(str(getdate(frozen_till)))
			),
			title=_("Accounts Frozen"),
		)

	closing = frappe.db.sql(
		"""
		SELECT MAX(period_end_date) AS period_end_date
		FROM `tabPeriod Closing Voucher`
		WHERE company = %(company)s AND docstatus = 1
		""",
		{"company": doc.company},
		as_dict=True,
	)
	last_closed_date = closing[0].period_end_date if closing else None
	if last_closed_date and (is_opening or posting_date <= getdate(last_closed_date)):
		frappe.throw(
			_("Accounting corrections cannot use a date in books closed through {0}.").format(
				frappe.bold(str(getdate(last_closed_date)))
			),
			title=_("Period Closed"),
		)

	closed_period = frappe.db.sql(
		"""
		SELECT ap.name
		FROM `tabAccounting Period` ap
		INNER JOIN `tabClosed Document` cd ON cd.parent = ap.name
		WHERE ap.company = %(company)s
			AND ap.disabled = 0
			AND cd.closed = 1
			AND cd.document_type = %(doctype)s
			AND %(posting_date)s BETWEEN ap.start_date AND ap.end_date
		LIMIT 1
		""",
		{
			"company": doc.company,
			"doctype": doc.doctype,
			"posting_date": posting_date,
		},
		as_dict=True,
	)
	if closed_period:
		frappe.throw(
			_("Accounting corrections cannot use the closed Accounting Period {0}.").format(
				frappe.bold(closed_period[0].name)
			),
			title=_("Closed Accounting Period"),
		)


def _exclude_invoice_payment(doc) -> dict:
	if doc.doctype != "Sales Invoice" or not cint(doc.is_pos):
		frappe.throw(_("This Sales Invoice does not currently include a POS payment."))

	old_allocations = _payment_allocation_records(doc, doc.payments)
	old_accounts = [row["account"] for row in old_allocations if row.get("account")]
	details = {
		"old_payment_terms_template": doc.get("payment_terms_template") or "",
		"old_payment_schedule": _payment_schedule_state(doc),
		"old_payment_accounts": old_accounts,
		"old_payment_allocations": old_allocations,
		"new_payment_allocations": [],
		"party_account": doc.debit_to,
		"change_account": doc.get("account_for_change_amount"),
		"old_paid_amount": flt(doc.paid_amount),
		"old_base_paid_amount": flt(doc.base_paid_amount),
		"old_change_amount": flt(doc.change_amount),
		"old_base_change_amount": flt(doc.base_change_amount),
		"new_change_amount": 0.0,
		"new_base_change_amount": 0.0,
		"old_outstanding_amount": flt(doc.outstanding_amount),
	}
	if cint(doc.get("is_return")) and doc.get("return_against") and not cint(
		doc.get("update_outstanding_for_self")
	):
		details["expected_outstanding_amount"] = 0.0
	else:
		paid_effect = (
			flt(doc.paid_amount)
			if doc.party_account_currency == doc.currency
			else flt(doc.base_paid_amount)
		)
		change_effect = (
			flt(doc.change_amount)
			if doc.party_account_currency == doc.currency
			else flt(doc.base_change_amount)
		)
		details["expected_outstanding_amount"] = flt(
			flt(doc.outstanding_amount) + paid_effect - change_effect,
			doc.precision("outstanding_amount"),
		)
	doc.is_pos = 0
	doc.set("payments", [])
	doc.paid_amount = 0
	doc.base_paid_amount = 0
	doc.change_amount = 0
	doc.base_change_amount = 0
	_rebuild_non_pos_payment_schedule(doc)
	details.update(
		{
			"expected_payment_terms_template": doc.get("payment_terms_template") or "",
			"expected_payment_schedule": _payment_schedule_state(doc),
		}
	)
	return details


def _invoice_include_amount(doc) -> float:
	if doc.doctype != "Sales Invoice" or cint(doc.is_pos):
		return 0.0
	outstanding = flt(doc.outstanding_amount, doc.precision("outstanding_amount"))
	if doc.party_account_currency == doc.currency:
		return flt(outstanding, doc.precision("paid_amount"))
	conversion_rate = flt(doc.conversion_rate)
	if not conversion_rate:
		frappe.throw(_("A valid Conversion Rate is required before payment can be included."))
	return flt(outstanding / conversion_rate, doc.precision("paid_amount"))


def _include_invoice_payment(doc, mode_of_payment: str | None) -> dict:
	if doc.doctype != "Sales Invoice" or cint(doc.is_pos):
		frappe.throw(_("This Sales Invoice already includes a POS payment."))
	if _invoice_payment_rows(doc):
		frappe.throw(
			_(
				"This non-POS invoice already contains payment rows. Correct that inconsistent source data before including payment."
			)
		)

	amount = _invoice_include_amount(doc)
	if not amount:
		frappe.throw(_("This invoice has no outstanding amount to include as payment."))

	mode = _get_mode_of_payment(doc.company, mode_of_payment)
	_validate_invoice_payment_account_currency(doc, mode.account)
	old_payment_terms_template = doc.get("payment_terms_template") or ""
	old_payment_schedule = _payment_schedule_state(doc)
	if doc.party_account_currency == doc.currency:
		base_amount = flt(amount * doc.conversion_rate, doc.precision("base_paid_amount"))
	else:
		# Outstanding Amount is denominated in party_account_currency. ERPNext
		# permits that currency to differ from the invoice only when the party
		# account is in company currency, so preserve that exact base balance.
		base_amount = flt(doc.outstanding_amount, doc.precision("base_paid_amount"))
	doc.is_pos = 1
	doc.set("payments", [])
	doc.append(
		"payments",
		{
			"mode_of_payment": mode.name,
			"account": mode.account,
			"type": mode.type,
			"amount": amount,
			"base_amount": base_amount,
			"default": 1,
		},
	)
	# A submitted update runs ``before_update_after_submit`` instead of the
	# normal Sales Invoice ``before_save`` path that calls ``set_paid_amount``.
	# Keep the parent totals in step with the payment child row explicitly.
	doc.paid_amount = amount
	doc.base_paid_amount = base_amount
	doc.change_amount = 0
	doc.base_change_amount = 0
	# ERPNext v17 treats an included Sales Invoice payment as POS accounting.
	# Keep the persisted terms state explicit because an update-after-submit does
	# not follow the normal draft lifecycle that originally established it.
	doc.payment_terms_template = ""
	doc.set("payment_schedule", [])
	return {
		"old_payment_terms_template": old_payment_terms_template,
		"old_payment_schedule": old_payment_schedule,
		"expected_payment_terms_template": "",
		"expected_payment_schedule": [],
		"new_mode_of_payment": mode.name,
		"target_account": mode.account,
		"party_account": doc.debit_to,
		"included_amount": amount,
		"base_included_amount": base_amount,
		"old_payment_allocations": [],
		"new_payment_allocations": _payment_allocation_records(doc, doc.payments),
		"change_account": doc.get("account_for_change_amount"),
		"old_change_amount": 0.0,
		"old_base_change_amount": 0.0,
		"new_change_amount": 0.0,
		"new_base_change_amount": 0.0,
	}


def _rebuild_non_pos_payment_schedule(doc):
	"""Recreate the standard v17 schedule after removing POS semantics."""
	doc.payment_terms_template = ""
	doc.set("payment_schedule", [])
	if cint(doc.get("is_return")) or doc.get("is_opening") == "Yes":
		return

	# Use the document controller rather than synthesizing a child row here. It
	# preserves ERPNext's due-date, currency, rounding and linked-order rules.
	doc.set_payment_schedule()
	if not doc.get("payment_schedule"):
		frappe.throw(
			_("A non-POS Sales Invoice must have a valid Payment Schedule."),
			title=_("Payment Schedule Missing"),
		)


def _change_invoice_mode_of_payment(
	doc,
	payment_row: str | None,
	new_mode_of_payment: str | None,
	payment_allocations: str | list[dict] | None = None,
) -> dict:
	if doc.doctype != "Sales Invoice" or not cint(doc.is_pos):
		frappe.throw(_("Mode of Payment can only be corrected on an invoice with Include Payment enabled."))

	active_rows = [row for row in doc.payments if flt(row.amount)]
	if not active_rows:
		frappe.throw(_("This invoice has no non-zero payment row to correct."))
	if any((row.get("reference_no") or "").strip() for row in doc.payments):
		frappe.throw(
			_(
				"This invoice has payment reference details. Unreconcile or review those references before changing its payment modes."
			),
			title=_("Payment Reference Dependency"),
		)
	if payment_allocations is not None:
		return _replace_invoice_payment_allocations(doc, active_rows, payment_allocations)

	if payment_row:
		matches = [row for row in active_rows if row.name == payment_row]
	elif len(active_rows) == 1:
		matches = active_rows
	else:
		matches = []
	if len(matches) != 1:
		frappe.throw(_("Select the payment row whose Mode of Payment should change."))

	old_allocations = _payment_allocation_records(doc, doc.payments)
	source = matches[0]
	old_account = source.account
	mode = _get_mode_of_payment(doc.company, new_mode_of_payment)
	if source.mode_of_payment == mode.name and source.account == mode.account:
		frappe.throw(_("Choose a different Mode of Payment."))
	_validate_same_account_currency(source.account, mode.account)

	# POS profiles commonly keep zero-value rows for every available mode. Reuse
	# such a row so the payment table remains tidy and the total stays identical.
	target = next(
		(
			row
			for row in doc.payments
			if row.name != source.name and row.mode_of_payment == mode.name and not flt(row.amount)
		),
		None,
	)
	if target:
		target.amount = source.amount
		target.base_amount = source.base_amount
		target.account = mode.account
		target.type = mode.type
		target.default = source.default
		source.amount = 0
		source.base_amount = 0
		source.default = 0
	else:
		source.mode_of_payment = mode.name
		source.account = mode.account
		source.type = mode.type

	return {
		"payment_row": source.name,
		"old_mode_of_payment": source.get_db_value("mode_of_payment"),
		"new_mode_of_payment": mode.name,
		"old_account": old_account,
		"target_account": mode.account,
		"party_account": doc.debit_to,
		"old_payment_allocations": old_allocations,
		"new_payment_allocations": _payment_allocation_records(doc, doc.payments),
		"preserved_paid_amount": flt(doc.paid_amount, doc.precision("paid_amount")),
		"preserved_base_paid_amount": flt(
			doc.base_paid_amount, doc.precision("base_paid_amount")
		),
		"change_account": doc.get("account_for_change_amount"),
		"old_change_amount": flt(doc.change_amount),
		"old_base_change_amount": flt(doc.base_change_amount),
		"new_change_amount": flt(doc.change_amount),
		"new_base_change_amount": flt(doc.base_change_amount),
	}


def _replace_invoice_payment_allocations(doc, active_rows, payment_allocations) -> dict:
	"""Replace the payment-mode distribution while preserving its exact total.

	This permits a cashier receipt to be split across modes (for example Cash +
	Mobile Money), but deliberately provides no way to increase or decrease the
	total amount recorded as paid.
	"""
	allocations = frappe.parse_json(payment_allocations)
	if not isinstance(allocations, list) or not allocations:
		frappe.throw(_("Add at least one payment allocation."))

	precision = doc.precision("paid_amount")
	base_precision = doc.precision("base_paid_amount")
	original_total = flt(sum(flt(row.amount) for row in active_rows), precision)
	if original_total != flt(doc.paid_amount, precision):
		frappe.throw(
			_("The existing payment rows do not agree with Paid Amount. Correct the source data first.")
		)
	original_base_total = flt(sum(flt(row.base_amount) for row in active_rows), base_precision)
	if original_base_total != flt(doc.base_paid_amount, base_precision):
		frappe.throw(
			_(
				"The existing payment rows do not agree with Base Paid Amount. Correct the source data first."
			)
		)
	if not original_total:
		frappe.throw(_("This invoice has no paid amount to redistribute."))

	direction = 1 if original_total > 0 else -1
	source_account = active_rows[0].account
	for source_row in active_rows[1:]:
		_validate_same_account_currency(source_account, source_row.account)
	combined: dict[str, dict] = {}
	for raw in allocations:
		row = frappe._dict(raw or {})
		amount = flt(row.amount, precision)
		if not amount:
			continue
		if amount * direction <= 0:
			frappe.throw(_("Every payment allocation must have the same sign as Paid Amount."))
		mode = _get_mode_of_payment(doc.company, row.mode_of_payment)
		_validate_invoice_payment_account_currency(doc, mode.account)
		_validate_same_account_currency(source_account, mode.account)
		if mode.name not in combined:
			combined[mode.name] = {
				"mode_of_payment": mode.name,
				"account": mode.account,
				"type": mode.type,
				"amount": 0.0,
			}
		combined[mode.name]["amount"] = flt(combined[mode.name]["amount"] + amount, precision)

	if not combined:
		frappe.throw(_("Add at least one non-zero payment allocation."))
	new_total = flt(sum(row["amount"] for row in combined.values()), precision)
	if new_total != original_total:
		frappe.throw(
			_("Payment allocations must total exactly {0}; the paid amount cannot be changed.").format(
				frappe.bold(frappe.format_value(original_total, {"fieldtype": "Currency"}))
			)
		)

	old_distribution = _aggregate_payment_distribution(active_rows, precision)
	new_distribution = _aggregate_payment_distribution(combined.values(), precision)
	if old_distribution == new_distribution:
		frappe.throw(_("Change at least one Mode of Payment or its allocation."))

	doc.set("payments", [])
	new_rows = list(combined.values())
	allocated_base_amount = 0.0
	for index, row in enumerate(new_rows):
		if index == len(new_rows) - 1:
			base_amount = flt(original_base_total - allocated_base_amount, base_precision)
		else:
			base_amount = flt(
				row["amount"] * flt(doc.conversion_rate or 1),
				base_precision,
			)
			allocated_base_amount = flt(allocated_base_amount + base_amount, base_precision)
		doc.append(
			"payments",
			{
				**row,
				"base_amount": base_amount,
				"default": 1 if index == 0 else 0,
			},
		)

	return {
		"old_payment_allocations": _payment_allocation_records(doc, active_rows),
		"new_payment_allocations": _payment_allocation_records(doc, doc.payments),
		"target_accounts": [row["account"] for row in combined.values()],
		"party_account": doc.debit_to,
		"preserved_paid_amount": original_total,
		"preserved_base_paid_amount": original_base_total,
		"change_account": doc.get("account_for_change_amount"),
		"old_change_amount": flt(doc.change_amount),
		"old_base_change_amount": flt(doc.base_change_amount),
		"new_change_amount": flt(doc.change_amount),
		"new_base_change_amount": flt(doc.base_change_amount),
	}


def _aggregate_payment_distribution(rows, precision: int) -> list[tuple[str, str, float]]:
	aggregated = {}
	for row in rows:
		key = (row.get("mode_of_payment"), row.get("account"))
		aggregated[key] = flt(aggregated.get(key, 0) + flt(row.get("amount")), precision)
	return sorted((*key, amount) for key, amount in aggregated.items() if amount)


def _payment_allocation_records(doc, rows) -> list[dict]:
	"""Capture active payment rows without relying on volatile child-row names."""
	precision = doc.precision("paid_amount")
	base_precision = doc.precision("base_paid_amount")
	return [
		{
			"mode_of_payment": row.get("mode_of_payment"),
			"account": row.get("account"),
			"amount": flt(row.get("amount"), precision),
			"base_amount": flt(row.get("base_amount"), base_precision),
		}
		for row in rows
		if flt(row.get("amount"), precision) or flt(row.get("base_amount"), base_precision)
	]


def _change_payment_entry_mode_of_payment(
	doc,
	new_mode_of_payment: str | None,
	account_side: str | None,
	new_reference_no: str | None,
	new_reference_date: str | None,
) -> dict:
	if doc.doctype != "Payment Entry":
		frappe.throw(_("This action is only available on Payment Entry."))
	if doc.payment_type == "Internal Transfer" and any(
		doc.get(fieldname) for fieldname in ("bank_account", "bank", "bank_account_no")
	):
		frappe.throw(
			_(
				"This Internal Transfer contains bank metadata whose account side is ambiguous. "
				"Review that metadata before changing its Mode of Payment."
			),
			title=_("Bank Metadata Dependency"),
		)

	fieldname = _payment_entry_account_field(doc, account_side)
	old_account = doc.get(fieldname)
	mode = _get_mode_of_payment(doc.company, new_mode_of_payment)
	if doc.mode_of_payment == mode.name and old_account == mode.account:
		frappe.throw(_("Choose a different Mode of Payment."))
	_validate_same_account_currency(old_account, mode.account)

	doc.mode_of_payment = mode.name
	doc.set(fieldname, mode.account)
	currency_field = f"{fieldname}_account_currency"
	doc.set(currency_field, mode.account_currency)
	doc.set(f"{fieldname}_account_type", mode.account_type)
	bank_metadata_synced = doc.payment_type in {"Pay", "Receive"}
	if bank_metadata_synced:
		_sync_company_bank_account_metadata(doc, mode.account)
	# ERPNext defines the transaction-reference side as paid_to for Receive and
	# paid_from for both Pay and Internal Transfer.  For Internal Transfer the
	# Mode of Payment therefore always controls paid_from; paid_to is not a mode
	# correction and must not drive whether bank references are kept.
	reference_account_field = "paid_to" if doc.payment_type == "Receive" else "paid_from"
	reference_account_type = doc.get(f"{reference_account_field}_account_type") or (
		frappe.get_cached_value("Account", doc.get(reference_account_field), "account_type")
	)
	if reference_account_type == "Bank":
		reference_no = (new_reference_no or doc.reference_no or "").strip()
		reference_date = getdate(new_reference_date or doc.reference_date) if (
			new_reference_date or doc.reference_date
		) else None
		if not reference_no or not reference_date:
			frappe.throw(_("Reference No and Reference Date are required for a bank payment."))
		doc.reference_no = reference_no
		doc.reference_date = reference_date
	else:
		# A reference belongs to the bank-side account being corrected. Keeping it
		# on a corrected Cash entry would leave misleading bank details behind.
		doc.reference_no = None
		doc.reference_date = None

	# Submitted-document updates do not run Payment Entry.validate(), where
	# ERPNext normally refreshes these automatic display fields.  Regenerate only
	# system-managed remarks; custom remarks must remain exactly as the user wrote
	# them.  An Internal Transfer title embeds both account names and therefore
	# must follow a paid-from account correction as well.
	if not cint(doc.get("custom_remarks")):
		doc.set_remarks()
	if doc.payment_type == "Internal Transfer":
		doc.set_title()
	return {
		"account_side": fieldname,
		"old_mode_of_payment": doc.get_db_value("mode_of_payment"),
		"new_mode_of_payment": mode.name,
		"old_account": old_account,
		"target_account": mode.account,
		"target_account_currency": mode.account_currency,
		"target_account_type": mode.account_type,
		"bank_metadata_synced": bank_metadata_synced,
		"bank_account": doc.get("bank_account"),
		"bank": doc.get("bank"),
		"bank_account_no": doc.get("bank_account_no"),
		"reference_no": doc.reference_no,
		"reference_date": doc.reference_date,
	}


def _sync_company_bank_account_metadata(doc, account: str):
	"""Keep Payment Entry's display metadata aligned with its ledger account."""
	bank_accounts = frappe.db.get_all(
		"Bank Account",
		filters={
			"company": doc.company,
			"account": account,
			"disabled": 0,
			"is_company_account": 1,
		},
		fields=["name", "bank", "bank_account_no", "is_default"],
		order_by="is_default desc, modified desc, name asc",
		limit=2,
	)
	if len(bank_accounts) > 1 and not (
		cint(bank_accounts[0].is_default) and not cint(bank_accounts[1].is_default)
	):
		frappe.throw(
			_(
				"More than one active company Bank Account uses {0}. Mark one as default before changing this payment."
			).format(frappe.bold(account))
		)

	bank_account = bank_accounts[0] if bank_accounts else frappe._dict()
	doc.bank_account = bank_account.get("name")
	doc.bank = bank_account.get("bank")
	doc.bank_account_no = bank_account.get("bank_account_no")


def _payment_entry_account_field(doc, account_side: str | None) -> str:
	if doc.payment_type == "Receive":
		return "paid_to"
	if doc.payment_type == "Pay":
		return "paid_from"
	if doc.payment_type == "Internal Transfer":
		if account_side not in {None, "", "paid_from"}:
			frappe.throw(
				_(
					"For an Internal Transfer, Mode of Payment can only change Account Paid From."
				)
			)
		return "paid_from"
	frappe.throw(_("Unsupported Payment Type {0}.").format(doc.payment_type))


def _get_mode_of_payment(company: str, mode_of_payment: str | None):
	if not mode_of_payment:
		frappe.throw(_("New Mode of Payment is required."))
	mode = frappe.db.get_value(
		"Mode of Payment",
		mode_of_payment,
		["name", "type", "enabled"],
		as_dict=True,
	)
	if not mode or not cint(mode.enabled):
		frappe.throw(_("Mode of Payment {0} is not enabled.").format(frappe.bold(mode_of_payment)))
	mode.account = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mode.name, "company": company},
		"default_account",
	)
	if not mode.account:
		frappe.throw(
			_("Mode of Payment {0} has no default account for {1}.").format(
				frappe.bold(mode.name), frappe.bold(company)
			)
		)
	account = frappe.db.get_value(
		"Account",
		mode.account,
		["company", "is_group", "disabled", "account_type", "account_currency"],
		as_dict=True,
	)
	if not account or account.company != company or cint(account.is_group) or cint(account.disabled):
		frappe.throw(_("The default account for Mode of Payment {0} is not usable.").format(mode.name))
	mode.account_type = account.account_type
	mode.account_currency = account.account_currency
	return mode


def _mode_of_payment_choices(company: str) -> list[dict]:
	return frappe.db.sql(
		"""
		SELECT mop.name, mop.type, mopa.default_account AS account,
		       account.account_currency, account.account_type
		FROM `tabMode of Payment` mop
		INNER JOIN `tabMode of Payment Account` mopa
			ON mopa.parent = mop.name AND mopa.company = %(company)s
		INNER JOIN `tabAccount` account ON account.name = mopa.default_account
		WHERE mop.enabled = 1 AND account.disabled = 0 AND account.is_group = 0
		ORDER BY mop.name
		""",
		{"company": company},
		as_dict=True,
	)


def _validate_same_account_currency(old_account: str, new_account: str):
	old_currency = frappe.get_cached_value("Account", old_account, "account_currency")
	new_currency = frappe.get_cached_value("Account", new_account, "account_currency")
	if old_currency != new_currency:
		frappe.throw(
			_(
				"The new payment account uses {0}, but the existing payment account uses {1}. "
				"Changing currency would also change the payment amount, which this correction does not allow."
			).format(frappe.bold(new_currency), frappe.bold(old_currency))
		)


def _validate_invoice_payment_account_currency(doc, account: str):
	account_currency = frappe.get_cached_value("Account", account, "account_currency")
	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")
	if account_currency not in {doc.currency, company_currency}:
		frappe.throw(
			_("The selected payment account currency must be {0} or {1}.").format(
				frappe.bold(doc.currency), frappe.bold(company_currency)
			)
		)


def _save_submitted_document(doc):
	doc.flags.ignore_validate_update_after_submit = True
	doc.flags.ignore_reposting_on_reconciliation = True
	doc.save(ignore_permissions=True, ignore_version=False)
	if doc.docstatus != 1:
		frappe.throw(_("The corrected document did not remain submitted."))


def _repost_accounting_ledger(doc) -> str:
	with _temporary_payment_entry_reconciliation_dates(doc):
		repost = frappe.new_doc("Repost Accounting Ledger")
		repost.company = doc.company
		repost.append("vouchers", {"voucher_type": doc.doctype, "voucher_no": doc.name})
		repost.flags.ignore_permissions = True
		repost.insert()
		repost.submit()
	return repost.name


@contextmanager
def _temporary_payment_entry_reconciliation_dates(doc):
	"""Let core replay legacy separate-advance rows without changing the source.

	Older Payment Entry Reference rows can predate ``reconcile_effect_on``. Core
	backfills that field from inside the GL posting routine. A correction must not
	leave that unrelated child-field change behind, so expose the calculated date
	only for the duration of the synchronous repost and restore its exact stored
	representation afterwards. ``update_modified=False`` also keeps the parent and
	child audit timestamps untouched.
	"""
	temporary_values = []
	try:
		if doc.doctype == "Payment Entry" and cint(
			doc.get("book_advance_payments_in_separate_party_account")
		):
			for reference in doc.get("references") or []:
				if (
					reference.get("reference_doctype")
					not in ADVANCE_RECONCILIATION_REFERENCE_DOCTYPES
					or reference.get("reconcile_effect_on")
					or not reference.get("name")
				):
					continue
				effect_date = _payment_entry_reference_effect_date(doc, reference)
				if not effect_date:
					continue
				temporary_values.append((reference.name, reference.get("reconcile_effect_on")))
				frappe.db.set_value(
					"Payment Entry Reference",
					reference.name,
					"reconcile_effect_on",
					effect_date,
					update_modified=False,
				)
		yield
	finally:
		for reference_name, original_value in reversed(temporary_values):
			frappe.db.set_value(
				"Payment Entry Reference",
				reference_name,
				"reconcile_effect_on",
				original_value,
				update_modified=False,
			)


def _protected_amounts(doc) -> dict:
	if doc.doctype == "Sales Invoice":
		return {field: flt(doc.get(field)) for field in (*SALES_TOTAL_FIELDS, *SALES_PAYMENT_TOTAL_FIELDS)}
	return {field: flt(doc.get(field)) for field in PAYMENT_ENTRY_AMOUNT_FIELDS}


def _validate_protected_amounts(doc, before: dict, action: str):
	if doc.doctype == "Sales Invoice":
		fields = list(SALES_TOTAL_FIELDS)
		if action not in {INCLUDE_PAYMENT, EXCLUDE_PAYMENT}:
			fields.extend(SALES_PAYMENT_TOTAL_FIELDS)
	else:
		fields = list(PAYMENT_ENTRY_AMOUNT_FIELDS)

	changed = [field for field in fields if abs(flt(doc.get(field)) - before[field]) > 0.000001]
	if changed:
		frappe.throw(
			_("This correction would change protected monetary fields: {0}.").format(
				", ".join(changed)
			)
		)


def _validate_protected_document(old_doc, new_doc, action: str, details: dict):
	"""Fail closed if normal save hooks alter anything outside the action scope."""
	old_state = _normalized_document(old_doc)
	new_state = _normalized_document(new_doc)
	allowed = {"modified", "modified_by"}

	if action == CHANGE_POSTING_DATE:
		allowed.update({"posting_date", "status"})
		if new_doc.doctype == "Sales Invoice":
			allowed.add("set_posting_time")
	elif new_doc.doctype == "Sales Invoice" and action in {INCLUDE_PAYMENT, EXCLUDE_PAYMENT}:
		allowed.update(
			{
				"is_pos",
				"payments",
				"payment_terms_template",
				"payment_schedule",
				"paid_amount",
				"base_paid_amount",
				"change_amount",
				"base_change_amount",
				"outstanding_amount",
				"status",
			}
		)
	elif new_doc.doctype == "Sales Invoice" and action == CHANGE_MODE_OF_PAYMENT:
		allowed.add("payments")
	elif new_doc.doctype == "Payment Entry" and action == CHANGE_MODE_OF_PAYMENT:
		account_field = details["account_side"]
		allowed.update(
			{
				"mode_of_payment",
				account_field,
				f"{account_field}_account_currency",
				f"{account_field}_account_type",
				"reference_no",
				"reference_date",
				"remarks",
				"title",
				"status",
			}
		)
		if details.get("bank_metadata_synced"):
			allowed.update({"bank_account", "bank", "bank_account_no"})

	for fieldname in allowed:
		old_state.pop(fieldname, None)
		new_state.pop(fieldname, None)

	if old_state != new_state:
		changed = sorted(
			fieldname
			for fieldname in set(old_state) | set(new_state)
			if old_state.get(fieldname) != new_state.get(fieldname)
		)
		frappe.throw(
			_("Correction stopped because unrelated fields would change: {0}.").format(
				", ".join(changed)
			),
			title=_("Unrelated Changes Detected"),
		)


def _normalized_document(doc) -> dict:
	data = frappe.parse_json(frappe.as_json(doc.as_dict(no_nulls=False)))

	def strip_runtime_metadata(value):
		if isinstance(value, list):
			return [strip_runtime_metadata(row) for row in value]
		if not isinstance(value, dict):
			return value
		return {
			key: strip_runtime_metadata(child)
			for key, child in value.items()
			if key not in {"modified", "modified_by"} and not key.startswith("__")
		}

	return strip_runtime_metadata(data)


def _invoice_payment_rows(doc) -> list[dict]:
	return [
		{
			"name": row.name,
			"mode_of_payment": row.mode_of_payment,
			"account": row.account,
			"type": row.get("type"),
			"default": cint(row.get("default")),
			"amount": flt(row.amount),
			"base_amount": flt(row.base_amount),
			"reference_no": row.get("reference_no"),
			"clearance_date": str(row.get("clearance_date") or ""),
		}
		for row in (doc.get("payments") or [])
		if flt(row.amount)
	]


def _payment_schedule_state(doc) -> list[dict]:
	"""Return stable, audit-safe values for every meaningful schedule field."""
	rows = []
	for row in doc.get("payment_schedule") or []:
		values = {}
		for fieldname in PAYMENT_SCHEDULE_FIELDS:
			value = row.get(fieldname)
			if fieldname == "idx":
				value = cint(value)
			elif fieldname in PAYMENT_SCHEDULE_NUMERIC_FIELDS:
				value = flt(value, 9)
			elif fieldname in {"due_date", "discount_date"}:
				value = str(value or "")
			elif fieldname == "discount_type":
				# New child rows receive this DocField default during insertion.
				# Normalize it before and after the database round-trip.
				value = value or "Percentage"
			else:
				value = value or ""
			values[fieldname] = value
		rows.append(values)
	return rows


def _document_state(doc) -> dict:
	state = {
		"docstatus": cint(doc.docstatus),
		"status": doc.get("status"),
		"posting_date": str(doc.posting_date),
		"amended_from": doc.get("amended_from"),
		"modified": str(doc.modified),
		"modified_by": doc.modified_by,
	}
	if doc.doctype == "Sales Invoice":
		state.update(
			{
				"posting_time": str(doc.get("posting_time") or ""),
				"set_posting_time": cint(doc.get("set_posting_time")),
				"currency": doc.currency,
				"party_account": doc.debit_to,
				"is_pos": cint(doc.is_pos),
				"paid_amount": flt(doc.paid_amount),
				"base_paid_amount": flt(doc.base_paid_amount),
				"change_amount": flt(doc.change_amount),
				"base_change_amount": flt(doc.base_change_amount),
				"outstanding_amount": flt(doc.outstanding_amount),
				"payments": _invoice_payment_rows(doc),
				"payment_terms_template": doc.get("payment_terms_template") or "",
				"payment_schedule": _payment_schedule_state(doc),
			}
		)
	else:
		state.update(
			{
				"payment_type": doc.payment_type,
				"mode_of_payment": doc.mode_of_payment,
				"paid_from": doc.paid_from,
				"paid_from_account_currency": doc.paid_from_account_currency,
				"paid_from_account_type": doc.paid_from_account_type,
				"paid_to": doc.paid_to,
				"paid_to_account_currency": doc.paid_to_account_currency,
				"paid_to_account_type": doc.paid_to_account_type,
				"reference_no": doc.reference_no,
				"reference_date": str(doc.reference_date or ""),
				"bank_account": doc.get("bank_account"),
				"bank": doc.get("bank"),
				"bank_account_no": doc.get("bank_account_no"),
				"clearance_date": str(doc.get("clearance_date") or ""),
				"remarks": doc.get("remarks"),
				"title": doc.get("title"),
				**{field: flt(doc.get(field)) for field in PAYMENT_ENTRY_AMOUNT_FIELDS},
			}
		)
	return state


def _ledger_snapshot(doc) -> dict:
	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
		get_accounting_dimensions,
	)

	accounting_dimensions = tuple(dict.fromkeys(get_accounting_dimensions() or []))
	gl_entries = frappe.db.get_all(
		"GL Entry",
		filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0},
		fields=[*GL_SNAPSHOT_FIELDS, *accounting_dimensions],
		order_by="creation, name",
	)
	ple_entries = frappe.db.get_all(
		"Payment Ledger Entry",
		filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "delinked": 0},
		fields=[*PAYMENT_LEDGER_SNAPSHOT_FIELDS, *accounting_dimensions],
		order_by="creation, name",
	)
	advance_entries = frappe.db.get_all(
		"Advance Payment Ledger Entry",
		filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "delinked": 0},
		fields=list(ADVANCE_PAYMENT_LEDGER_SNAPSHOT_FIELDS),
		order_by="creation, name",
	)
	sle_entries = []
	if doc.doctype == "Sales Invoice" and cint(doc.get("update_stock")):
		sle_entries = frappe.db.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0},
			fields=[
				"posting_date",
				"posting_time",
				"item_code",
				"warehouse",
				"actual_qty",
				"qty_after_transaction",
				"incoming_rate",
				"outgoing_rate",
				"valuation_rate",
				"stock_value",
				"stock_value_difference",
				"stock_queue",
				"serial_and_batch_bundle",
				"voucher_detail_no",
			],
			order_by="creation, name",
		)

	return {
		"accounting_dimensions": list(accounting_dimensions),
		"gl_entries": gl_entries,
		"gl_debit": sum(flt(row.debit) for row in gl_entries),
		"gl_credit": sum(flt(row.credit) for row in gl_entries),
		"payment_ledger_entries": ple_entries,
		"advance_payment_ledger_entries": advance_entries,
		"stock_ledger_entries": sle_entries,
	}


def _expected_processed_gl_batches(doc) -> list[list[dict]]:
	"""Replay the pure GL-map pipeline used by the document controller."""
	from erpnext.accounts.general_ledger import (
		make_acc_dimensions_offsetting_entry,
		process_debit_credit_difference,
		process_gl_map,
	)

	working_doc = copy.deepcopy(doc)
	entry_batches = []

	if working_doc.doctype == "Sales Invoice":
		# Repost Accounting Ledger calls force_set_against_income_account before
		# posting.  Its in-memory counterpart is sufficient here and avoids a DB
		# write during verification.
		working_doc.set_against_income_account()
		entry_batches.append((working_doc.get_gl_entries(), False))
	elif working_doc.doctype == "Payment Entry":
		merge_entries = cint(
			frappe.get_single_value("Accounts Settings", "merge_similar_account_heads")
		)
		# Payment Entry pre-processes its regular map once before passing it to
		# the general-ledger writer, which processes it a second time.
		regular_entries = process_gl_map(
			working_doc.build_gl_map(), merge_entries=merge_entries
		)
		entry_batches.append((regular_entries, merge_entries))

		if cint(working_doc.get("book_advance_payments_in_separate_party_account")):
			_prepare_advance_reconciliation_dates(working_doc)
			advance_entries = []
			working_doc.add_advance_gl_entries(advance_entries, None)
			entry_batches.append((advance_entries, True))
	else:
		frappe.throw(_("Correction verification failed: unsupported ledger document."))

	processed_batches = []
	for raw_entries, merge_entries in entry_batches:
		entries = copy.deepcopy(raw_entries)
		if not entries:
			continue
		make_acc_dimensions_offsetting_entry(entries)
		entries = process_gl_map(entries, merge_entries=merge_entries)
		if not entries:
			continue
		process_debit_credit_difference(entries)
		processed_batches.append(entries)
	return processed_batches


def _expected_active_gl_entries(doc) -> list[dict]:
	"""Regenerate the exact GL rows that ERPNext posts for the current document.

	The verification must not infer a payment correction from account totals:
	totals cannot detect a wrong party, dimension, counterpart, or an extra pair
	of balanced rows. Replaying ERPNext's map gives an authoritative full-row
	expectation while leaving the submitted document and database untouched.
	"""
	expected = []
	for entries in _expected_processed_gl_batches(doc):
		for values in entries:
			# New-document defaults and GL validation populate is_advance, fiscal
			# year and all reporting-currency values exactly as insertion does.
			gle = frappe.new_doc("GL Entry")
			gle.update(values)
			gle.flags.from_repost = False
			gle.validate()
			gle.docstatus = 1
			expected.append(gle)

	return expected


def _prepare_advance_reconciliation_dates(doc):
	"""Populate a copied PE without triggering core's compatibility DB write."""
	for reference in doc.get("references") or []:
		if (
			reference.get("reconcile_effect_on")
			or reference.get("reference_doctype")
			not in ADVANCE_RECONCILIATION_REFERENCE_DOCTYPES
		):
			continue
		reference.reconcile_effect_on = _payment_entry_reference_effect_date(doc, reference)


def _payment_entry_reference_effect_date(doc, reference):
	if reference.get("reconcile_effect_on"):
		return getdate(reference.get("reconcile_effect_on"))
	from erpnext.accounts.utils import get_reconciliation_effect_date

	return get_reconciliation_effect_date(
		reference.reference_doctype,
		reference.reference_name,
		doc.company,
		doc.posting_date,
	)


def _payment_entry_ledger_posting_dates(doc) -> set:
	"""Return every legitimate date in a Payment Entry's GL/PLE replay."""
	dates = {getdate(doc.posting_date)}
	if not cint(doc.get("book_advance_payments_in_separate_party_account")):
		return dates
	for reference in doc.get("references") or []:
		if reference.get("reference_doctype") not in ADVANCE_RECONCILIATION_REFERENCE_DOCTYPES:
			continue
		if effect_date := _payment_entry_reference_effect_date(doc, reference):
			dates.add(getdate(effect_date))
	return dates


def _canonical_gl_rows(rows, accounting_dimensions) -> list[str]:
	fields = tuple(dict.fromkeys((*GL_SNAPSHOT_FIELDS, *(accounting_dimensions or []))))
	return _canonical_projected_rows(rows, fields, GL_NUMERIC_FIELDS)


def _canonical_projected_rows(rows, fields, numeric_fields) -> list[str]:
	canonical = []
	for row in rows:
		values = {}
		for fieldname in fields:
			value = row.get(fieldname)
			if fieldname in numeric_fields:
				value = flt(value, 9)
			elif value is None:
				value = ""
			values[fieldname] = value
		canonical.append(frappe.as_json(dict(sorted(values.items()))))
	return sorted(canonical)


def _expected_active_payment_ledgers(doc) -> tuple[list[dict], list[dict]]:
	from erpnext.accounts.utils import get_payment_ledger_entries

	payment_entries = []
	advance_entries = []
	for gl_entries in _expected_processed_gl_batches(doc):
		for entry in get_payment_ledger_entries(gl_entries, cancel=0):
			entry.docstatus = 1
			if entry.doctype == "Payment Ledger Entry":
				payment_entries.append(entry)
			elif entry.doctype == "Advance Payment Ledger Entry":
				advance_entries.append(entry)
	return payment_entries, advance_entries


def _verify_active_gl_matches_document(doc, snapshot: dict):
	"""Require every active accounting-ledger row to match the source document."""
	dimensions = snapshot.get("accounting_dimensions") or []
	expected = _expected_active_gl_entries(doc)
	actual = snapshot.get("gl_entries") or []
	if _canonical_gl_rows(actual, dimensions) != _canonical_gl_rows(expected, dimensions):
		frappe.throw(
			_(
				"Correction verification failed: active General Ledger rows do not exactly match the corrected document."
			)
		)

	expected_ple, expected_aple = _expected_active_payment_ledgers(doc)
	ple_fields = tuple(dict.fromkeys((*PAYMENT_LEDGER_SNAPSHOT_FIELDS, *dimensions)))
	if _canonical_projected_rows(
		snapshot.get("payment_ledger_entries") or [],
		ple_fields,
		PAYMENT_LEDGER_NUMERIC_FIELDS,
	) != _canonical_projected_rows(
		expected_ple,
		ple_fields,
		PAYMENT_LEDGER_NUMERIC_FIELDS,
	):
		frappe.throw(
			_(
				"Correction verification failed: active Payment Ledger rows do not exactly match the corrected document."
			)
		)

	if _canonical_projected_rows(
		snapshot.get("advance_payment_ledger_entries") or [],
		ADVANCE_PAYMENT_LEDGER_SNAPSHOT_FIELDS,
		ADVANCE_PAYMENT_LEDGER_NUMERIC_FIELDS,
	) != _canonical_projected_rows(
		expected_aple,
		ADVANCE_PAYMENT_LEDGER_SNAPSHOT_FIELDS,
		ADVANCE_PAYMENT_LEDGER_NUMERIC_FIELDS,
	):
		frappe.throw(
			_(
				"Correction verification failed: active Advance Payment Ledger rows do not exactly match the corrected document."
			)
		)


def _verify_result(doc, action: str, details: dict, before: dict, after: dict):
	if doc.docstatus != 1:
		frappe.throw(_("Correction verification failed: document is no longer submitted."))
	if before["gl_entries"] and not after["gl_entries"]:
		frappe.throw(_("Correction verification failed: active General Ledger entries are missing."))
	if abs(flt(after["gl_debit"]) - flt(after["gl_credit"])) > 0.01:
		frappe.throw(_("Correction verification failed: General Ledger debits and credits do not balance."))
	_verify_active_gl_matches_document(doc, after)

	if action == CHANGE_POSTING_DATE:
		if doc.doctype == "Sales Invoice" and cint(doc.get("update_stock")):
			frappe.throw(
				_("Correction verification failed: stock-backed invoice dates are not supported.")
			)
		expected = getdate(details["new_posting_date"])
		if getdate(doc.posting_date) != expected:
			frappe.throw(_("Correction verification failed: the document still has the old date."))
		valid_ledger_dates = (
			_payment_entry_ledger_posting_dates(doc)
			if doc.doctype == "Payment Entry"
			else {expected}
		)
		for ledger_name in ("gl_entries", "payment_ledger_entries", "stock_ledger_entries"):
			wrong_dates = [
				row.posting_date
				for row in after[ledger_name]
				if getdate(row.posting_date) not in valid_ledger_dates
			]
			if wrong_dates:
				frappe.throw(
					_("Correction verification failed: {0} contains an unexpected date.").format(
						ledger_name.replace("_", " ").title()
					)
				)

	target_accounts = set(details.get("target_accounts") or [])
	if details.get("target_account"):
		target_accounts.add(details["target_account"])
	for target_account in target_accounts:
		if after["gl_entries"] and not any(row.account == target_account for row in after["gl_entries"]):
			frappe.throw(
				_("Correction verification failed: payment account {0} is absent from the ledger.").format(
					frappe.bold(target_account)
				)
			)
	if action == CHANGE_MODE_OF_PAYMENT:
		if doc.doctype == "Sales Invoice":
			_verify_invoice_payment_distribution(doc, details)
		else:
			account_side = details["account_side"]
			# Link/Data fields can round-trip SQL NULL as an empty string.  Those
			# representations are equivalent here; compare their normalized values
			# while still rejecting any substantive metadata drift.
			bank_metadata_matches = all(
				(doc.get(fieldname) or "") == (details.get(fieldname) or "")
				for fieldname in ("bank_account", "bank", "bank_account_no")
			)
			if (
				doc.mode_of_payment != details["new_mode_of_payment"]
				or doc.get(account_side) != details["target_account"]
				or doc.get(f"{account_side}_account_currency")
				!= details["target_account_currency"]
				or doc.get(f"{account_side}_account_type") != details["target_account_type"]
				or doc.reference_no != details["reference_no"]
				or str(doc.reference_date or "") != str(details["reference_date"] or "")
				or not bank_metadata_matches
			):
				frappe.throw(
					_("Correction verification failed: the Payment Entry mode details were not preserved.")
				)

	if action == INCLUDE_PAYMENT:
		precision = doc.precision("paid_amount")
		base_precision = doc.precision("base_paid_amount")
		payment_rows = _invoice_payment_rows(doc)
		if (
			not cint(doc.is_pos)
			or not payment_rows
			or flt(doc.paid_amount, precision) != flt(details["included_amount"], precision)
			or flt(doc.base_paid_amount, base_precision)
			!= flt(details["base_included_amount"], base_precision)
			or flt(sum(row["amount"] for row in payment_rows), precision)
			!= flt(details["included_amount"], precision)
			or flt(sum(row["base_amount"] for row in payment_rows), base_precision)
			!= flt(details["base_included_amount"], base_precision)
			or len(payment_rows) != 1
			or payment_rows[0]["mode_of_payment"] != details["new_mode_of_payment"]
			or payment_rows[0]["account"] != details["target_account"]
			or flt(doc.outstanding_amount, doc.precision("outstanding_amount")) != 0
		):
			frappe.throw(_("Correction verification failed: payment was not included."))
	if action == EXCLUDE_PAYMENT:
		if (
			cint(doc.is_pos)
			or _invoice_payment_rows(doc)
			or flt(doc.paid_amount)
			or flt(doc.base_paid_amount)
			or flt(doc.change_amount)
			or flt(doc.base_change_amount)
			or flt(doc.outstanding_amount, doc.precision("outstanding_amount"))
			!= flt(details["expected_outstanding_amount"], doc.precision("outstanding_amount"))
		):
			frappe.throw(_("Correction verification failed: payment was not excluded."))

	if doc.doctype == "Sales Invoice" and action in {INCLUDE_PAYMENT, EXCLUDE_PAYMENT}:
		_verify_invoice_payment_schedule_transition(doc, action, details)
		_verify_invoice_payment_distribution(doc, details)

	if doc.doctype == "Sales Invoice" and action != CHANGE_POSTING_DATE:
		_assert_ledger_rows_equal_except(
			before["stock_ledger_entries"],
			after["stock_ledger_entries"],
			set(),
			"Stock Ledger",
		)


def _verify_invoice_payment_distribution(
	doc,
	details: dict,
):
	"""Verify that active payment rows retain the authorized exact total."""
	precision = doc.precision("paid_amount")
	base_precision = doc.precision("base_paid_amount")
	expected_rows = details.get("new_payment_allocations")
	if expected_rows is None:
		frappe.throw(
			_("Correction verification failed: expected payment distribution is missing.")
		)

	def canonical(rows):
		result = []
		for row in rows:
			mode_of_payment = row.get("mode_of_payment")
			account = row.get("account")
			amount = flt(row.get("amount"), precision)
			base_amount = flt(row.get("base_amount"), base_precision)
			if not amount and not base_amount:
				continue
			if not mode_of_payment or not account:
				frappe.throw(
					_("Correction verification failed: an active payment row is incomplete.")
				)
			result.append((mode_of_payment, account, amount, base_amount))
		return sorted(result)

	expected = canonical(expected_rows)
	actual = canonical(doc.get("payments") or [])
	if actual != expected:
		frappe.throw(
			_("Correction verification failed: the invoice payment distribution is not exact.")
		)

	expected_paid = flt(sum(row[2] for row in expected), precision)
	expected_base_paid = flt(sum(row[3] for row in expected), base_precision)
	if (
		flt(doc.paid_amount, precision) != expected_paid
		or flt(doc.base_paid_amount, base_precision) != expected_base_paid
	):
		frappe.throw(
			_("Correction verification failed: payment rows do not match the invoice totals.")
		)

	if details.get("preserved_paid_amount") is not None and (
		expected_paid != flt(details["preserved_paid_amount"], precision)
		or expected_base_paid
		!= flt(details.get("preserved_base_paid_amount"), base_precision)
	):
		frappe.throw(
			_("Correction verification failed: the payment total was not preserved.")
		)

def _verify_invoice_payment_schedule_transition(doc, action: str, details: dict):
	expected_template = details.get("expected_payment_terms_template")
	expected_schedule = details.get("expected_payment_schedule")
	if expected_template is None or expected_schedule is None:
		frappe.throw(
			_("Correction verification failed: expected Payment Schedule state is missing.")
		)

	actual_template = doc.get("payment_terms_template") or ""
	actual_schedule = _payment_schedule_state(doc)
	if actual_template != expected_template or actual_schedule != expected_schedule:
		frappe.throw(
			_("Correction verification failed: Payment Schedule did not make the exact expected transition.")
		)
	if action == INCLUDE_PAYMENT and (actual_template or actual_schedule):
		frappe.throw(
			_("Correction verification failed: a POS invoice retained payment terms.")
		)
	if (
		action == EXCLUDE_PAYMENT
		and not cint(doc.get("is_return"))
		and doc.get("is_opening") != "Yes"
		and not actual_schedule
	):
		frappe.throw(
			_("Correction verification failed: the non-POS invoice has no Payment Schedule.")
		)


def _assert_ledger_rows_equal_except(
	before_rows, after_rows, ignored_fields, ledger_label, summed_fields=None
):
	summed_fields = summed_fields or set()

	def canonical(rows):
		if not summed_fields:
			return sorted(
				frappe.as_json(
					dict(
						sorted(
							(key, value)
							for key, value in dict(row).items()
							if key not in ignored_fields
						)
					)
				)
				for row in rows
			)

		grouped = {}
		for row in rows:
			value = dict(row)
			for fieldname in ignored_fields:
				value.pop(fieldname, None)
			amounts = {fieldname: flt(value.pop(fieldname, 0), 9) for fieldname in summed_fields}
			key = frappe.as_json(dict(sorted(value.items())))
			grouped.setdefault(key, {fieldname: 0.0 for fieldname in summed_fields})
			for fieldname, amount in amounts.items():
				grouped[key][fieldname] = flt(grouped[key][fieldname] + amount, 9)
		return sorted((key, tuple(sorted(amounts.items()))) for key, amounts in grouped.items())

	if canonical(before_rows) != canonical(after_rows):
		frappe.throw(
			_("Correction verification failed: unrelated {0} values changed.").format(ledger_label)
		)


def _create_audit_log(
	*,
	doc,
	action: str,
	reason: str,
	key: str,
	old_values: dict,
	new_values: dict,
	ledger_before: dict,
	ledger_after: dict,
	repost_reference: str,
):
	log = frappe.get_doc(
		{
			"doctype": AUDIT_DOCTYPE,
			"reference_doctype": doc.doctype,
			"reference_name": doc.name,
			"action": ACTION_LABELS[action],
			"company": doc.company,
			"reason": reason,
			"old_values": frappe.as_json(old_values, indent=2),
			"new_values": frappe.as_json(new_values, indent=2),
			"ledger_before": frappe.as_json(ledger_before, indent=2),
			"ledger_after": frappe.as_json(ledger_after, indent=2),
			"performed_by": frappe.session.user,
			"performed_on": now_datetime(),
			"client_ip": getattr(frappe.local, "request_ip", None),
			"repost_reference": repost_reference,
			"unique_action_key": key,
		}
	)
	log.insert(ignore_permissions=True)
	return log


def _add_timeline_comment(doc, log, action: str, reason: str, old_values: dict, new_values: dict):
	changed = _human_change_summary(action, old_values, new_values)
	log_link = frappe.utils.get_link_to_form(AUDIT_DOCTYPE, log.name)
	doc.add_comment(
		"Info",
		_("{0} performed by {1}. {2}<br>Reason: {3}<br>Audit: {4}").format(
			frappe.bold(ACTION_LABELS[action]),
			frappe.bold(frappe.session.user),
			changed,
			frappe.utils.escape_html(reason),
			log_link,
		),
	)


def _human_change_summary(action: str, old_values: dict, new_values: dict) -> str:
	if action == CHANGE_POSTING_DATE:
		return _("Posting Date: {0} → {1}.").format(
			frappe.bold(old_values["posting_date"]), frappe.bold(new_values["posting_date"])
		)
	if action in {INCLUDE_PAYMENT, EXCLUDE_PAYMENT}:
		return _("Include Payment: {0} → {1}; Paid Amount: {2} → {3}.").format(
			frappe.bold(old_values["is_pos"]),
			frappe.bold(new_values["is_pos"]),
			frappe.bold(old_values["paid_amount"]),
			frappe.bold(new_values["paid_amount"]),
		)
	return _("Mode/account details changed; all payment amounts were preserved.")

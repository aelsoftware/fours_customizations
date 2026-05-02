from pydoc import doc

import frappe
from frappe.utils import flt
from frappe.model.mapper import get_mapped_doc
from erpnext.accounts.party import get_party_account


def _is_automation_enabled(company):
	"""Check if selling automations are enabled for this company."""
	return frappe.db.get_value("Company", company, "enable_selling_automations")


def before_submit(doc, method=None):
	doc.update_outstanding_for_self = 0


def before_save(doc, method=None):
	"""Auto-enable advance allocation and prevent Include Payment from forcing update_stock on."""
	if _is_automation_enabled(doc.company):
		doc.allocate_advances_automatically = 1
		# ERPNext auto-sets update_stock=1 when Include Payment (is_pos) is enabled.
		# Override that: disable update_stock and set warehouse so the auto-created
		# Delivery Note (on_submit) picks up the correct warehouse for its items.
		if doc.is_pos:
			doc.update_stock = 0
			doc.set_posting_time = 1
			doc.set_warehouse = "Main Store - 4S"
			for item in doc.items:
				item.warehouse = "Main Store - 4S"

	doc.update_outstanding_for_self = 0
	if doc.is_return or doc.return_against:
		dn_parents = frappe.get_all(
			"Delivery Note Item",
			filters={
				"against_sales_invoice": doc.return_against
			},
			pluck="parent"
		)

		if not dn_parents:
			return

		draft_dn = frappe.db.get_value(
			"Delivery Note",
			{
				"docstatus": 0,
				"name": ["in", dn_parents]
			},
			"name",
		)

		if draft_dn:
			full_name = frappe.db.get_value("User", frappe.session.user, "full_name") or "User"
			# Split and get last name
			name_parts = full_name.strip().split()
			last_name = name_parts[-1] if len(name_parts) > 1 else full_name

			frappe.throw(
				f"{last_name} you make a cancellation request instead. Delivery Note {draft_dn} is still in draft."
			)


def on_submit(doc, method=None):
	"""Create Sales Order, draft Delivery Note and handle advance/credit note commission on submit."""
	if not _is_automation_enabled(doc.company):
		return

	if not doc.is_return:
		so_name = _create_sales_order_from_invoice(doc)
		_create_draft_delivery_note(doc, sales_order=so_name)
		_create_advance_commission(doc)
	else:
		_create_draft_delivery_note(doc)
		_create_credit_note_commission(doc)


def before_cancel(doc, method=None):
	doc.flags.ignore_links = True
    
	"""Clear all back-references on this SI and suppress link validation
	before ERPNext's validator runs."""
	# Clear SO/DN links on SI item rows so the validator does not find
	# SI → SO or SI → DN connections that would block the cancel.
	for item in doc.items:
		frappe.db.set_value(
			"Sales Invoice Item",
			item.name,
			{
				"sales_order": None,
				"so_detail": None,
				"dn_detail": None,
				"delivery_note": None,
			},
			update_modified=False,
		)

	# Always suppress link validation — we have cleared the item-level links
	# above, but the validator also checks reverse references (JEs, DN items,
	# etc.) that cannot all be cleared without amending submitted documents.


def on_cancel(doc, method=None):
	"""Create reversal JEs for all commission JEs linked to this Sales Invoice."""
	if not _is_automation_enabled(doc.company):
		return

	from fours_customizations.gl_entry_handler import _create_reversal_je

	commission_jes = frappe.get_all("Journal Entry", filters={
		"custom_commission_sales_invoice": doc.name,
		"docstatus": 1,
		"custom_commission_voucher_no": ["not like", "REV-%"],
	}, pluck="name")

	for je_name in commission_jes:
		_create_reversal_je(je_name, reason=f"invoice {doc.name} cancelled")


# ──────────────────────────────────────────────────────────────────────────────
# Sales Order creation
# ──────────────────────────────────────────────────────────────────────────────

def _create_sales_order_from_invoice(doc):
	"""
	Create a submitted Sales Order mirroring the Sales Invoice, then back-link
	every SI item to that SO so the Delivery Note can be raised against it.

	Returns the new SO name, or None if creation was skipped.
	"""
	if not doc.items:
		return None

	# Only create for stock items (same filter used for the DN)
	stock_item_codes = {
		row[0] for row in frappe.get_all(
			"Item",
			filters={
				"name": ("in", [item.item_code for item in doc.items if item.item_code]),
				"is_stock_item": 1,
			},
			fields=["name"],
			as_list=True,
		)
	}

	if not stock_item_codes:
		return None

	eligible_items = [
		item for item in doc.items
		if item.item_code in stock_item_codes
		and not getattr(item, "sales_order", None)  # not already linked to an SO
		and flt(item.qty) > 0
	]

	if not eligible_items:
		return None

	# Duplicate guard: one SO per SI
	existing_so = frappe.db.get_value(
		"Sales Order",
		{"custom_sales_invoice": doc.name, "docstatus": ["!=", 2]},
		"name",
	)
	if existing_so:
		frappe.msgprint(f"Sales Order {existing_so} already exists for {doc.name}.", alert=True)
		return existing_so

	so = frappe.new_doc("Sales Order")
	so.customer = doc.customer
	so.customer_name = doc.customer_name
	so.company = doc.company
	so.transaction_date = doc.posting_date
	so.delivery_date = doc.posting_date  # deliver same day (adjust if needed)
	so.currency = doc.currency
	so.conversion_rate = doc.conversion_rate
	so.selling_price_list = doc.selling_price_list
	so.price_list_currency = doc.price_list_currency
	so.plc_conversion_rate = doc.plc_conversion_rate
	so.ignore_pricing_rule = 1
	so.set_warehouse = doc.set_warehouse or "Main Store - 4S"
	so.cost_center = doc.cost_center
	so.sales_partner = doc.sales_partner
	so.commission_rate = doc.commission_rate
	so.total_commission = doc.total_commission
	so.letter_head = doc.letter_head
	so.tc_name = getattr(doc, "tc_name", None)
	so.custom_sales_invoice = doc.name  # back-link (custom field — see install note below)

	for item in eligible_items:
		so.append("items", {
			"item_code": item.item_code,
			"item_name": item.item_name,
			"description": item.description,
			"qty": flt(item.qty),
			"uom": item.uom,
			"stock_uom": item.stock_uom,
			"conversion_factor": flt(item.conversion_factor) or 1,
			"rate": flt(item.rate),
			"price_list_rate": flt(item.price_list_rate) or flt(item.rate),
			"warehouse": item.warehouse or so.set_warehouse,
			"cost_center": item.cost_center or doc.cost_center,
			"delivery_date": doc.posting_date,
		})

	so.flags.ignore_permissions = True
	so.flags.ignore_mandatory = True
	so.insert()
	so.submit()

	# Mark every SO item as fully billed since the SI is already submitted.
	# ERPNext normally does this via update_billed_amount_in_so when an SI is
	# submitted against an existing SO, but here the SO is created after the fact
	# so we set billed_amt = base_amount (qty × base_rate) directly.
	for so_item in so.items:
		for si_item in eligible_items:
			if si_item.item_code == so_item.item_code:
				frappe.db.set_value(
					"Sales Order Item",
					so_item.name,
					"billed_amt",
					flt(so_item.qty) * flt(so_item.base_rate),
					update_modified=False,
				)
				break

	# Back-link SI items → SO + SO item for DN linkage
	for so_item in so.items:
		for si_item in eligible_items:
			if (
				si_item.item_code == so_item.item_code
				and not getattr(si_item, "sales_order", None)
			):
				si_item.sales_order = so.name
				si_item.so_detail = so_item.name
				break

	# Persist the SI item updates without triggering full hooks
	for si_item in eligible_items:
		if getattr(si_item, "sales_order", None) == so.name:
			frappe.db.set_value(
				"Sales Invoice Item",
				si_item.name,
				{
					"sales_order": si_item.sales_order,
					"so_detail": si_item.so_detail,
				},
				update_modified=False,
			)

	frappe.msgprint(f"Sales Order {so.name} created and linked to {doc.name}.", alert=True)
	return so.name


# ──────────────────────────────────────────────────────────────────────────────
# Commission helpers (unchanged)
# ──────────────────────────────────────────────────────────────────────────────

def _create_credit_note_commission(doc):
	"""Create a negative commission JE when a credit note (return SI) is submitted."""
	if not doc.sales_partner or flt(doc.total_commission) >= 0:
		return

	commission = abs(flt(doc.total_commission, 2))
	if commission <= 0:
		return

	# Duplicate prevention
	if frappe.db.exists("Journal Entry", {
		"custom_commission_voucher_no": doc.name,
		"custom_commission_sales_invoice": doc.name,
		"docstatus": ["!=", 2],
	}):
		return

	supplier = frappe.db.get_value("Sales Partner", doc.sales_partner, "custom_supplier_account")
	if not supplier:
		frappe.msgprint(
			f"Sales Partner {doc.sales_partner} has no linked Supplier (custom_supplier_account). "
			"Skipping commission Journal Entry.",
			alert=True,
		)
		return

	expense_account = frappe.db.get_value(
		"Company", doc.company, "sales_commission_expense_account"
	)
	if not expense_account:
		frappe.throw(
			f"Please configure the Sales Commission Expense Account on the Selling Automations tab "
			f"in Company {doc.company} before submitting."
		)

	creditors_account = get_party_account("Supplier", supplier, doc.company)
	cost_center = doc.cost_center or frappe.get_cached_value("Company", doc.company, "cost_center")

	original_si = doc.return_against or ""

	je = frappe.get_doc({
		"doctype": "Journal Entry",
		"voucher_type": "Journal Entry",
		"posting_date": doc.posting_date,
		"company": doc.company,
		"user_remark": f"Commission reduction for credit note {doc.name} against {original_si}",
		"custom_commission_sales_invoice": doc.name,
		"custom_commission_voucher_no": doc.name,
		"accounts": [
			{
				"account": creditors_account,
				"debit_in_account_currency": commission,
				"party_type": "Supplier",
				"party": supplier,
			},
			{
				"account": expense_account,
				"credit_in_account_currency": commission,
				"cost_center": cost_center,
			},
		],
	})
	je.insert(ignore_permissions=True)
	je.submit()

	frappe.msgprint(
		f"Commission reduction Journal Entry {je.name} created for credit note {doc.name} ({commission:,.2f}).",
		alert=True,
	)


def _create_advance_commission(doc):
	"""Create commission JEs for advance payments allocated to this SI."""
	if not doc.sales_partner or flt(doc.total_commission) <= 0:
		return

	if flt(doc.base_grand_total) <= 0:
		return

	for advance in doc.advances:
		if flt(advance.allocated_amount) <= 0:
			continue

		voucher_no = advance.reference_name

		# Duplicate prevention
		if frappe.db.exists("Journal Entry", {
			"custom_commission_voucher_no": voucher_no,
			"custom_commission_sales_invoice": doc.name,
			"docstatus": ["!=", 2],
		}):
			continue

		paid_ratio = min(flt(advance.allocated_amount) / flt(doc.base_grand_total), 1.0)
		commission = flt(paid_ratio * flt(doc.total_commission), 2)

		if commission <= 0:
			continue

		supplier = frappe.db.get_value("Sales Partner", doc.sales_partner, "custom_supplier_account")
		if not supplier:
			frappe.msgprint(
				f"Sales Partner {doc.sales_partner} has no linked Supplier (custom_supplier_account). "
				"Skipping commission Journal Entry.",
				alert=True,
			)
			return

		expense_account = frappe.db.get_value(
			"Company", doc.company, "sales_commission_expense_account"
		)
		if not expense_account:
			frappe.throw(
				f"Please configure the Sales Commission Expense Account on the Selling Automations tab "
				f"in Company {doc.company} before submitting."
			)

		creditors_account = get_party_account("Supplier", supplier, doc.company)
		cost_center = doc.cost_center or frappe.get_cached_value("Company", doc.company, "cost_center")

		je = frappe.get_doc({
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"posting_date": doc.posting_date,
			"company": doc.company,
			"user_remark": f"Commission for {voucher_no} (advance) allocation to {doc.name}",
			"custom_commission_payment_entry": voucher_no if advance.reference_type == "Payment Entry" else None,
			"custom_commission_sales_invoice": doc.name,
			"custom_commission_voucher_no": voucher_no,
			"accounts": [
				{
					"account": expense_account,
					"debit_in_account_currency": commission,
					"cost_center": cost_center,
				},
				{
					"account": creditors_account,
					"credit_in_account_currency": commission,
					"party_type": "Supplier",
					"party": supplier,
				},
			],
		})
		je.insert(ignore_permissions=True)
		je.submit()

		frappe.msgprint(f"Commission Journal Entry {je.name} created for advance on {doc.name}.", alert=True)


# ──────────────────────────────────────────────────────────────────────────────
# Delivery Note creation
# ──────────────────────────────────────────────────────────────────────────────

def _create_draft_delivery_note(doc, sales_order=None):
	"""Create a draft Delivery Note from the Sales Invoice, linked via the Sales Order."""
	if doc.update_stock:
		return

	if not doc.items:
		return

	# For normal invoices, skip if all items already have a DN link
	if not doc.is_return and all(getattr(item, "dn_detail", None) for item in doc.items):
		return

	# Only include items that maintain stock
	stock_item_codes = {
		row[0] for row in frappe.get_all(
			"Item",
			filters={
				"name": ("in", [item.item_code for item in doc.items if item.item_code]),
				"is_stock_item": 1,
			},
			fields=["name"],
			as_list=True,
		)
	}

	if not stock_item_codes:
		return

	if doc.is_return:
		_create_draft_delivery_note_return(doc, stock_item_codes)
		return

	# Normal invoice flow
	has_eligible_items = any(
		item.item_code in stock_item_codes
		and not getattr(item, "dn_detail", None)
		and flt(item.qty) - flt(item.delivered_qty) > 0
		and not item.get("delivered_by_supplier")
		for item in doc.items
	)
	if not has_eligible_items:
		return

	def set_missing_values(source, target):
		target.run_method("set_missing_values")
		target.run_method("set_po_nos")
		target.run_method("calculate_taxes_and_totals")

	def update_item(source_doc, target_doc, source_parent):
		target_doc.qty = flt(source_doc.qty) - flt(source_doc.delivered_qty)
		target_doc.stock_qty = target_doc.qty * flt(source_doc.conversion_factor)
		target_doc.base_amount = target_doc.qty * flt(source_doc.base_rate)
		target_doc.amount = target_doc.qty * flt(source_doc.rate)
		# Link DN item → SO item if the SI item was linked to the SO above
		if getattr(source_doc, "sales_order", None):
			target_doc.against_sales_order = source_doc.sales_order
			target_doc.so_detail = source_doc.so_detail

	dn = get_mapped_doc(
		"Sales Invoice",
		doc.name,
		{
			"Sales Invoice": {
				"doctype": "Delivery Note",
				"validation": {"docstatus": ["=", 1]},
			},
			"Sales Invoice Item": {
				"doctype": "Delivery Note Item",
				"field_map": {
					"name": "si_detail",
					"parent": "against_sales_invoice",
					"serial_no": "serial_no",
					"sales_order": "against_sales_order",
					"so_detail": "so_detail",
					"cost_center": "cost_center",
				},
				"postprocess": update_item,
				"condition": lambda item: (
					item.item_code in stock_item_codes
					and not item.get("delivered_by_supplier")
					and not getattr(item, "dn_detail", None)
					and flt(item.qty) - flt(item.delivered_qty) > 0
				),
			},
			"Sales Taxes and Charges": {
				"doctype": "Sales Taxes and Charges",
				"reset_value": True,
			},
			"Sales Team": {
				"doctype": "Sales Team",
				"field_map": {"incentives": "incentives"},
				"add_if_empty": True,
			},
		},
		None,
		set_missing_values,
	)

	if dn and dn.items:
		dn.set_posting_time = 1
		dn.posting_date = doc.posting_date
		dn.posting_time = doc.posting_time

		# If a Sales Order was created, set it at the DN header level too
		if sales_order:
			for dn_item in dn.items:
				if not dn_item.against_sales_order:
					dn_item.against_sales_order = sales_order

		dn.insert(ignore_permissions=True)

		# Write dn_detail back onto each SI item row so that:
		#  - delivered_qty is calculated correctly
		#  - the duplicate-DN guard (dn_detail check above) works on re-runs
		#  - the DN link is visible in the SI item table
		si_detail_to_dn_item = {dn_item.si_detail: dn_item.name for dn_item in dn.items}
		for si_item in doc.items:
			dn_item_name = si_detail_to_dn_item.get(si_item.name)
			if dn_item_name:
				frappe.db.set_value(
					"Sales Invoice Item",
					si_item.name,
					{
						"dn_detail": dn_item_name,
						"delivery_note": dn.name,
					},
					update_modified=False,
				)

		frappe.msgprint(f"Draft Delivery Note {dn.name} created.", alert=True)


def _create_draft_delivery_note_return(doc, stock_item_codes):
	"""Create a draft Delivery Note Return from a return Sales Invoice."""
	if not doc.return_against:
		return

	# prevent duplicate draft DN return for the same SI return
	existing_dn = frappe.db.get_value(
		"Delivery Note",
		{
			"docstatus": 0,
			"is_return": 1,
			"return_against": ["is", "set"],
			"custom_remarks": ["like", f"%Auto-created from Sales Invoice Return {doc.name}%"]
		},
		"name",
	)
	if existing_dn:
		return

	returned_items = [
		item for item in doc.items
		if item.item_code in stock_item_codes
		and not item.get("delivered_by_supplier")
		and flt(item.qty) < 0
		and getattr(item, "sales_invoice_item", None)
	]

	if not returned_items:
		return

	original_si_item_names = [item.sales_invoice_item for item in returned_items if item.sales_invoice_item]
	if not original_si_item_names:
		return

	dn_items = frappe.get_all(
		"Delivery Note Item",
		filters={
			"docstatus": 1,
			"si_detail": ["in", original_si_item_names],
		},
		fields=[
			"name",
			"parent",
			"item_code",
			"rate",
			"warehouse",
			"cost_center",
			"si_detail",
			"against_sales_invoice",
			"against_sales_order",
			"so_detail",
			"uom",
			"stock_uom",
			"conversion_factor",
		],
	)

	if not dn_items:
		return

	dn_item_map = {}
	for row in dn_items:
		dn_item_map.setdefault(row.parent, []).append(row)

	returned_si_item_map = {
		item.sales_invoice_item: item
		for item in returned_items
		if item.sales_invoice_item
	}

	created_dns = []

	for original_dn_name, original_dn_items in dn_item_map.items():
		already_exists = frappe.db.get_value(
			"Delivery Note",
			{
				"docstatus": 0,
				"is_return": 1,
				"return_against": original_dn_name,
				"custom_remarks": ["like", f"%Auto-created from Sales Invoice Return {doc.name}%"],
			},
			"name",
		)
		if already_exists:
			created_dns.append(already_exists)
			continue

		original_dn = frappe.get_doc("Delivery Note", original_dn_name)

		dn_return = frappe.new_doc("Delivery Note")
		dn_return.is_return = 1
		dn_return.return_against = original_dn.name
		dn_return.naming_series = original_dn.naming_series
		dn_return.customer = original_dn.customer
		dn_return.customer_name = original_dn.customer_name
		dn_return.company = original_dn.company
		dn_return.posting_date = doc.posting_date
		dn_return.posting_time = doc.posting_time
		dn_return.set_posting_time = 1
		dn_return.currency = original_dn.currency
		dn_return.conversion_rate = original_dn.conversion_rate
		dn_return.selling_price_list = original_dn.selling_price_list
		dn_return.price_list_currency = original_dn.price_list_currency
		dn_return.plc_conversion_rate = original_dn.plc_conversion_rate
		dn_return.ignore_pricing_rule = 1
		dn_return.set_warehouse = original_dn.set_warehouse
		dn_return.cost_center = original_dn.cost_center
		dn_return.company_address = original_dn.company_address
		dn_return.sales_partner = original_dn.sales_partner
		dn_return.letter_head = original_dn.letter_head
		dn_return.custom_creator_name = getattr(original_dn, "custom_creator_name", None)
		dn_return.custom_employee_name = getattr(original_dn, "custom_employee_name", None)
		dn_return.custom_approver_name = getattr(original_dn, "custom_approver_name", None)
		dn_return.custom_deliverer_name = getattr(original_dn, "custom_deliverer_name", None)
		dn_return.custom_remarks = f"Auto-created from Sales Invoice Return {doc.name}"

		for dn_item in original_dn_items:
			si_return_item = returned_si_item_map.get(dn_item.si_detail)
			if not si_return_item:
				continue

			dn_return.append("items", {
				"item_code": si_return_item.item_code,
				"item_name": si_return_item.item_name,
				"description": si_return_item.description,
				"qty": flt(si_return_item.qty),
				"uom": si_return_item.uom or dn_item.uom,
				"stock_uom": si_return_item.stock_uom or dn_item.stock_uom,
				"conversion_factor": flt(si_return_item.conversion_factor or dn_item.conversion_factor or 1),
				"warehouse": dn_item.warehouse or si_return_item.warehouse or original_dn.set_warehouse,
				"rate": flt(dn_item.rate or si_return_item.rate),
				"price_list_rate": flt(dn_item.rate or si_return_item.price_list_rate or si_return_item.rate),
				"cost_center": dn_item.cost_center or original_dn.cost_center,
				"against_sales_invoice": doc.return_against,
				"against_sales_order": dn_item.against_sales_order,
				"so_detail": dn_item.so_detail,
				"si_detail": dn_item.si_detail,
				"dn_detail": dn_item.name,
				"use_serial_batch_fields": getattr(si_return_item, "use_serial_batch_fields", 0),
			})

		if not dn_return.items:
			continue

		dn_return.flags.ignore_permissions = True
		dn_return.insert()
		created_dns.append(dn_return.name)

	if created_dns:
		frappe.msgprint(
			f"Draft Delivery Note Return {', '.join(created_dns)} created.",
			alert=True,
		)
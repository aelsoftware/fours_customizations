import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def after_install():
	"""Create custom fields, salary components, and seed singletons after install/migrate."""
	create_designation_custom_fields()
	create_salary_components()
	create_sales_invoice_custom_fields()
	create_payment_entry_custom_fields()
	create_landed_cost_voucher_custom_fields()
	create_item_custom_fields()
	create_sales_order_custom_fields()
	create_company_custom_fields()
	seed_settings_defaults()


def create_designation_custom_fields():
	"""Add attendance deduction fields to Designation doctype"""

	custom_fields = {
		"Designation": [
			{
				"fieldname": "attendance_deductions_section",
				"label": "Attendance Deductions",
				"fieldtype": "Section Break",
				"insert_after": "description",
				"collapsible": 0,
			},
			{
				"fieldname": "attendance_deductions_note",
				"label": "Note",
				"fieldtype": "HTML",
				"insert_after": "attendance_deductions_section",
				"options": "<p style='color: #888;'>Set the deduction amounts for attendance violations. These amounts will be deducted per occurrence from employee salaries.</p>",
			},
			{
				"fieldname": "absent_deduction",
				"label": "Absent Deduction",
				"fieldtype": "Currency",
				"insert_after": "attendance_deductions_note",
				"description": "Amount to deduct per absence occurrence",
				"precision": 2,
			},
			{
				"fieldname": "column_break_deductions",
				"fieldtype": "Column Break",
				"insert_after": "absent_deduction",
			},
			{
				"fieldname": "late_deduction",
				"label": "Late Deduction",
				"fieldtype": "Currency",
				"insert_after": "column_break_deductions",
				"description": "Amount to deduct per late arrival occurrence",
				"precision": 2,
			},
			{
				"fieldname": "section_break_deductions_2",
				"fieldtype": "Section Break",
				"insert_after": "late_deduction",
			},
			{
				"fieldname": "early_exit_deduction",
				"label": "Early Exit Deduction",
				"fieldtype": "Currency",
				"insert_after": "section_break_deductions_2",
				"description": "Amount to deduct per early exit occurrence",
				"precision": 2,
			},
			{
				"fieldname": "column_break_deductions_2",
				"fieldtype": "Column Break",
				"insert_after": "early_exit_deduction",
			},
			{
				"fieldname": "no_checkout_deduction",
				"label": "Employee Doesn't Checkout Deduction",
				"fieldtype": "Currency",
				"insert_after": "column_break_deductions_2",
				"description": "Amount to deduct when employee doesn't checkout",
				"precision": 2,
			},
			{
				"fieldname": "overtime_configuration_section",
				"label": "Overtime Configuration",
				"fieldtype": "Section Break",
				"insert_after": "no_checkout_deduction",
				"collapsible": 1,
			},
			{
				"fieldname": "overtime_configuration_note",
				"label": "Note",
				"fieldtype": "HTML",
				"insert_after": "overtime_configuration_section",
				"options": "<p style='color: #888;'>Set the overtime window and hourly rate for this designation. Overtime will be calculated between the start and end times, and capped at the end time.</p>",
			},
			{
				"fieldname": "overtime_start_time",
				"label": "Overtime Start Time",
				"fieldtype": "Time",
				"insert_after": "overtime_configuration_note",
				"description": "Time when overtime calculation begins (e.g., 17:00:00 for 5:00 PM)",
			},
			{
				"fieldname": "column_break_overtime",
				"fieldtype": "Column Break",
				"insert_after": "overtime_start_time",
			},
			{
				"fieldname": "overtime_end_time",
				"label": "Overtime End Time",
				"fieldtype": "Time",
				"insert_after": "column_break_overtime",
				"description": "Maximum time for overtime calculation (e.g., 22:00:00 for 10:00 PM). Work beyond this time will not be paid.",
			},
			{
				"fieldname": "section_break_overtime_2",
				"fieldtype": "Section Break",
				"insert_after": "overtime_end_time",
			},
			{
				"fieldname": "overtime_hourly_rate",
				"label": "Overtime Hourly Rate",
				"fieldtype": "Currency",
				"insert_after": "section_break_overtime_2",
				"description": "Amount to pay per hour of overtime worked",
				"precision": 2,
			},
		]
	}

	create_custom_fields(custom_fields, update=True)
	frappe.db.commit()


def create_salary_components():
	"""Create salary components for attendance deductions, overtime, and sales commission."""

	components = [
		{"salary_component": "Absent Deduction", "type": "Deduction"},
		{"salary_component": "Late Deduction", "type": "Deduction"},
		{"salary_component": "Early Exit Deduction", "type": "Deduction"},
		{"salary_component": "No Checkout Deduction", "type": "Deduction"},
		{"salary_component": "Designation Overtime Pay", "type": "Earning"},
		{"salary_component": "Sales Commission", "type": "Earning"},
	]

	for comp_data in components:
		if not frappe.db.exists("Salary Component", comp_data["salary_component"]):
			frappe.get_doc({
				"doctype": "Salary Component",
				"salary_component": comp_data["salary_component"],
				"type": comp_data["type"],
			}).insert(ignore_permissions=True)

	frappe.db.commit()


def create_sales_invoice_custom_fields():
	"""Add custom fields to Sales Invoice.

	Note: commission Journal Entry tracking fields were removed when we moved
	commission entirely to the Salary Slip path.  We use the standard
	`amount_eligible_for_commission` field on the SI rather than a custom one.
	"""

	custom_fields = {
		"Sales Invoice": [
			{
				"fieldname": "custom_sales_person",
				"label": "Sales Person",
				"fieldtype": "Link",
				"options": "Sales Person",
				"insert_after": "sales_partner",
				"description": "When set, the Sales Team is automatically populated with this person at 100% allocation. Sits just before Amount Eligible for Commission.",
			},
			{
				"fieldname": "custom_auto_created_sales_order",
				"label": "Auto-created Sales Order",
				"fieldtype": "Link",
				"options": "Sales Order",
				"insert_after": "total_commission",
				"read_only": 1,
				"no_copy": 1,
			},
		],
	}

	create_custom_fields(custom_fields, update=True)
	frappe.db.commit()


def create_payment_entry_custom_fields():
	"""Add the canonical `sales_person` Link field on Payment Entry.

	Commission is now driven entirely by who collected the payment.
	The standard Sales Person doctype already links to an Employee and carries
	a `commission_rate`, so no further custom fields are needed.
	"""

	custom_fields = {
		"Payment Entry": [
			{
				"fieldname": "sales_person",
				"label": "Sales Person",
				"fieldtype": "Link",
				"options": "Sales Person",
				"insert_after": "reference_no",
				"description": "Sales Person who collected this payment. Drives commission for their Salary Slip.",
			},
		],
	}

	create_custom_fields(custom_fields, update=True)
	frappe.db.commit()


def create_company_custom_fields():
	"""Keep the Selling Automations toggle on Company — it gates SO/DN creation
	for the company. The commission-account field is no longer needed (no JEs)."""

	custom_fields = {
		"Company": [
			{
				"fieldname": "selling_automations_tab",
				"label": "Selling Automations",
				"fieldtype": "Tab Break",
				"insert_after": "purchase_expense_contra_account",
			},
			{
				"fieldname": "enable_selling_automations",
				"label": "Enable Selling Automations",
				"fieldtype": "Check",
				"insert_after": "selling_automations_tab",
				"description": "Enable auto Sales Order creation, draft Delivery Notes, and advance allocation on Sales Invoices.",
			},
		],
	}

	create_custom_fields(custom_fields, update=True)
	frappe.db.commit()


def create_landed_cost_voucher_custom_fields():
	"""Add 'New Selling Price' column to LCV items so submit can sync Item Price."""

	custom_fields = {
		"Landed Cost Item": [
			{
				"fieldname": "new_selling_price",
				"label": "New Selling Price",
				"fieldtype": "Currency",
				"insert_after": "applicable_charges",
				"description": "If set, the standard selling Item Price for this item is overwritten when the LCV is submitted.",
			},
		],
	}

	create_custom_fields(custom_fields, update=True)
	frappe.db.commit()


def create_item_custom_fields():
	"""Track reconciliation timestamps + cause of negative-stock enablement."""

	custom_fields = {
		"Item": [
			{
				"fieldname": "custom_last_reconciliation_request",
				"label": "Last Reconciliation Request",
				"fieldtype": "Datetime",
				"insert_after": "stock_uom",
				"read_only": 1,
				"no_copy": 1,
				"description": "Last time the system asked for reconciliation of this item.",
			},
			{
				"fieldname": "custom_negative_stock_auto_enabled",
				"label": "Negative Stock Auto-Enabled",
				"fieldtype": "Check",
				"insert_after": "custom_last_reconciliation_request",
				"read_only": 1,
				"no_copy": 1,
				"description": "Set when the system temporarily enabled negative stock on this item. Cleared after reconciliation.",
			},
		],
	}

	create_custom_fields(custom_fields, update=True)
	frappe.db.commit()


def create_sales_order_custom_fields():
	"""Track the originating SI on auto-created Sales Orders."""

	custom_fields = {
		"Sales Order": [
			{
				"fieldname": "custom_source_sales_invoice",
				"label": "Source Sales Invoice",
				"fieldtype": "Link",
				"options": "Sales Invoice",
				"insert_after": "amended_from",
				"read_only": 1,
				"no_copy": 1,
				"allow_on_submit": 1,
			},
		],
	}

	create_custom_fields(custom_fields, update=True)
	frappe.db.commit()


def seed_settings_defaults():
	"""Ensure the 4S Industries Settings single doc exists with sane defaults."""
	if not frappe.db.exists("DocType", "4S Industries Settings"):
		return

	try:
		settings = frappe.get_doc("4S Industries Settings")
	except frappe.DoesNotExistError:
		settings = frappe.new_doc("4S Industries Settings")

	if not settings.default_company:
		company = frappe.defaults.get_global_default("company") or frappe.db.get_value("Company", {}, "name")
		if company:
			settings.default_company = company

	settings.flags.ignore_validate = True
	settings.flags.ignore_permissions = True
	settings.save(ignore_permissions=True)
	frappe.db.commit()

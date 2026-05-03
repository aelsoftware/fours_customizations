app_name = "fours_customizations"
app_title = "Fours Customizations"
app_publisher = "Frappe"
app_description = "Custom app for attendance deduction management"
app_email = "elvisndegwa90@gmail.com"
app_license = "mit"

# Installation
# ------------
after_install = "fours_customizations.install.after_install"
after_migrate = "fours_customizations.install.after_install"

# include js in doctype views
doctype_js = {"Sales Invoice": "public/js/sales_invoice.js"}

# Document Events
# ---------------
doc_events = {
	"Salary Slip": {
		"before_save": "fours_customizations.salary_slip_handler.calculate_and_add_deductions",
		"before_insert": "fours_customizations.salary_slip_handler.calculate_and_add_deductions",
	},
	"Sales Invoice": {
		"on_submit": "fours_customizations.sales_invoice_handler.on_submit",
		"before_submit": "fours_customizations.sales_invoice_handler.before_submit",
		"before_save": "fours_customizations.sales_invoice_handler.before_save",
		# "before_cancel": "fours_customizations.sales_invoice_handler.before_cancel",
		# "on_cancel": "fours_customizations.sales_invoice_handler.on_cancel",
		"before_insert": "fours_customizations.sales_invoice_handler.before_insert",
		"before_validate": "fours_customizations.sales_invoice_handler.before_validate",
	},
	"Sales Order": {
		# "before_cancel": "fours_customizations.sales_order_handler.before_cancel",
		# "on_cancel": "fours_customizations.sales_order_handler.on_cancel",
	},
	"Delivery Note": {
		# "before_cancel": "fours_customizations.delivery_note_handler.before_cancel",
		# "on_cancel": "fours_customizations.delivery_note_handler.on_cancel",
		# "on_trash": "fours_customizations.delivery_note_handler.on_trash",
	},
	"Payment Entry": {
		"before_submit": "fours_customizations.payment_entry_handler.before_submit",
		"before_cancel": "fours_customizations.payment_entry_handler.before_cancel",
	},
	"GL Entry": {
		"on_submit": "fours_customizations.gl_entry_handler.on_submit",
	},
	"Unreconcile Payment": {
		"on_submit": "fours_customizations.gl_entry_handler.on_unreconcile",
	},
}
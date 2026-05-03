app_name = "fours_customizations"
app_title = "Fours Customizations"
app_publisher = "Frappe"
app_description = "Custom app for attendance deduction management"
app_email = "elvisndegwa90@gmail.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "fours_customizations",
# 		"logo": "/assets/fours_customizations/logo.png",
# 		"title": "Fours Customizations",
# 		"route": "/fours_customizations",
# 		"has_permission": "fours_customizations.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/fours_customizations/css/fours_customizations.css"
# app_include_js = "/assets/fours_customizations/js/fours_customizations.js"

# include js, css files in header of web template
# web_include_css = "/assets/fours_customizations/css/fours_customizations.css"
# web_include_js = "/assets/fours_customizations/js/fours_customizations.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "fours_customizations/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {"Sales Invoice": "public/js/sales_invoice.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "fours_customizations/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "fours_customizations.utils.jinja_methods",
# 	"filters": "fours_customizations.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "fours_customizations.install.before_install"
after_install = "fours_customizations.install.after_install"
after_migrate = "fours_customizations.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "fours_customizations.uninstall.before_uninstall"
# after_uninstall = "fours_customizations.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "fours_customizations.utils.before_app_install"
# after_app_install = "fours_customizations.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "fours_customizations.utils.before_app_uninstall"
# after_app_uninstall = "fours_customizations.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "fours_customizations.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {}

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"Salary Slip": {
		"before_save": "fours_customizations.salary_slip_handler.calculate_and_add_deductions",
		"before_insert": "fours_customizations.salary_slip_handler.calculate_and_add_deductions",
	},
	"Sales Invoice": {
		"on_submit": "fours_customizations.sales_invoice_handler.on_submit",
		"before_submit": "fours_customizations.sales_invoice_handler.before_submit",
		"before_save": "fours_customizations.sales_invoice_handler.before_save",
		"before_cancel": "fours_customizations.sales_invoice_handler.before_cancel",
		"on_cancel": "fours_customizations.sales_invoice_handler.on_cancel",
	},
	"Sales Order": {
		"before_submit": "fours_customizations.sales_order_handler.before_submit",
		"on_submit": "fours_customizations.sales_order_handler.on_submit",
		"on_cancel": "fours_customizations.sales_order_handler.on_cancel",
	},
	"Delivery Note": {
		"on_trash": "fours_customizations.delivery_note_handler.on_trash",
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

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"fours_customizations.tasks.all"
# 	],
# 	"daily": [
# 		"fours_customizations.tasks.daily"
# 	],
# 	"hourly": [
# 		"fours_customizations.tasks.hourly"
# 	],
# 	"weekly": [
# 		"fours_customizations.tasks.weekly"
# 	],
# 	"monthly": [
# 		"fours_customizations.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "fours_customizations.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "fours_customizations.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "fours_customizations.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["fours_customizations.utils.before_request"]
# after_request = ["fours_customizations.utils.after_request"]

# Job Events
# ----------
# before_job = ["fours_customizations.utils.before_job"]
# after_job = ["fours_customizations.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"fours_customizations.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }
import os as _os

app_name = "ez_accounting"
app_title = "Ez Accounting"
app_publisher = "Yulin He"
app_description = "Practical accounting workflow extensions for ERPNext — Express Sales, Deposits, Bank Reconciliation."
app_email = "yulinhe88@gmail.com"
app_license = "mit"


def _asset_version(relative_path: str) -> str:
	"""Cache-busting query param for a raw (non-bundled) static asset. Frappe's
	own bundler only versions "*.bundle.*" files (see
	frappe.utils.jinja_globals.bundled_asset) — forms_portal.css is referenced
	directly instead, so without this a browser could keep serving a stale
	cached copy indefinitely after an edit. Computed once here, at hooks.py
	import time (so a worker restart — e.g. from `bench build`/`bench
	migrate`/a deploy — is what actually picks up a new value, not every
	request), from the file's own mtime.
	"""
	try:
		path = _os.path.join(_os.path.dirname(__file__), relative_path)
		return str(int(_os.path.getmtime(path)))
	except OSError:
		return "0"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "forms",
# 		"logo": "/assets/ez_accounting/logo.png",
# 		"title": "Forms",
# 		"route": "/forms",
# 		"has_permission": "ez_accounting.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/ez_accounting/css/forms.css"
# app_include_js = "/assets/ez_accounting/js/forms.js"

# include js, css files in header of web template
# Font Awesome isn't loaded on website pages by default (only bundled into
# desk.bundle.scss for Desk) — button_icons.html's <i class="fa fa-..."> icons
# need it included explicitly here, same as frappe/www/attribution.html does.
web_include_css = [
	"/assets/frappe/css/fonts/fontawesome/font-awesome.min.css",
	"/assets/ez_accounting/css/forms_daisy.css?v=" + _asset_version("public/css/forms_daisy.css"),
	"/assets/ez_accounting/css/forms_portal.css?v=" + _asset_version("public/css/forms_portal.css"),
]
# Self-guards to /forms/* only (see forms_portal.js) — turns the portal's
# app-wide "save-status is-saved" inline convention into a toast on every
# auto-save. Same raw-asset cache-busting as forms_portal.css above.
# forms_quick_jump.js is a separate, unrelated concern (the Ctrl/Cmd+K
# global command palette) — kept as its own file rather than bolted onto
# forms_portal.js, same cache-busting. forms_martin.js (the "mini Martin"
# floating AI chat button) is another such independent, site-wide widget.
web_include_js = [
	"/assets/ez_accounting/js/forms_portal.js?v=" + _asset_version("public/js/forms_portal.js"),
	"/assets/ez_accounting/js/forms_quick_jump.js?v=" + _asset_version("public/js/forms_quick_jump.js"),
	"/assets/ez_accounting/js/forms_martin.js?v=" + _asset_version("public/js/forms_martin.js"),
]

# Website Route Rules
# -------------------
# Multi-company URL prefix: /c/<abbr>/forms/user/dashboard resolves to the same
# page as /forms/user/dashboard, but with the company pinned in the URL (so
# different browser tabs can hold different companies at once — see
# ez_accounting.api.request_forms_company / get_selected_company). One rule per real
# portal page, generated from the www/forms tree so a new page is covered
# automatically on the next worker restart. `to_route` can't interpolate the
# captured abbr, hence the explicit per-page list; the abbr is read back from
# frappe.local.request.path, not the (rewritten-away) route. A bare /forms/...
# URL with no prefix keeps working exactly as before (per-user default).
def _forms_page_routes() -> list[str]:
	base = _os.path.join(_os.path.dirname(__file__), "www", "forms")
	routes = []
	for root, _dirs, files in _os.walk(base):
		for f in files:
			if not f.endswith(".html"):
				continue
			rel = _os.path.relpath(_os.path.join(root, f), _os.path.join(_os.path.dirname(__file__), "www"))
			routes.append(rel[: -len(".html")])
	return sorted(routes)


website_route_rules = [
	# A company's bare root is its dashboard. Keep both shapes because the
	# browser/CDN may preserve or remove the trailing slash.
	{"from_route": "/c/<forms_company_abbr>", "to_route": "forms/user/dashboard"},
	{"from_route": "/c/<forms_company_abbr>/", "to_route": "forms/user/dashboard"},
	*[
	{"from_route": "/c/<forms_company_abbr>/" + route, "to_route": route}
	for route in _forms_page_routes()
	],
]

# Website Context
# ----------------
# Replace the default navbar "My Account" (/me) link with /forms/user/me,
# scoped to /forms/* pages only (see forms/website_context.py).
update_website_context = ["ez_accounting.website_context.update_website_context"]

# Renders the friendly www/forms/no-access.html page (403) when a user hits a
# /forms/user|admin/ page they're not allowed — instead of Frappe's generic
# "Not Permitted" screen. See forms/no_access_renderer.py.
page_renderer = ["ez_accounting.no_access_renderer.FormsNoAccessPage"]

permission_query_conditions = {
	"Forms File Record": "ez_accounting.file_access.permission_query_conditions",
	"Forms Company Document": "ez_accounting.doc_access.permission_query_conditions",
}
has_permission = {
	"Forms File Record": "ez_accounting.file_access.has_permission",
	"Forms Company Document": "ez_accounting.doc_access.has_permission",
}

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "forms/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "forms/public/icons.svg"

# Home Pages
# ----------

# User-aware application homepage: see ez_accounting.website_context. Guests need the
# login page; authenticated users land in the Forms portal dashboard.
get_website_user_home_page = "ez_accounting.website_context.get_website_user_home_page"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "ez_accounting.utils.jinja_methods",
# 	"filters": "ez_accounting.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "ez_accounting.install.before_install"
# after_install = "ez_accounting.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "ez_accounting.uninstall.before_uninstall"
# after_uninstall = "ez_accounting.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "ez_accounting.utils.before_app_install"
# after_app_install = "ez_accounting.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "ez_accounting.utils.before_app_uninstall"
# after_app_uninstall = "ez_accounting.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "ez_accounting.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "ez_accounting.notifications.get_notification_config"

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

# Document Events
# ---------------
# Hook on document methods and events

# Gives every Payment Entry / Forms Bank Deposit Sheet a permanent,
# per-company receipt/voucher number the first time it's submitted —
# <abbr>-00001, <abbr>-DP-00001, ... — via Frappe's own atomic Series
# counter. Fires regardless of which page created the document (Express
# Sales/Purchase, Payroll, a plain Desk entry, ...), so no individual call
# site has to remember to do this itself. See ez_accounting.api.
# assign_payment_sequence_number / assign_deposit_sequence_number,
# /forms/user/payments, /forms/user/deposits.
doc_events = {
	"Communication": {
		"on_update": "ez_accounting.www.forms.admin.email.archive_xerox_communication",
	},
	"Payment Entry": {
		"on_submit": "ez_accounting.api.assign_payment_sequence_number",
	},
	"Forms Bank Deposit Sheet": {
		"on_submit": "ez_accounting.api.assign_deposit_sequence_number",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"ez_accounting.tasks.enqueue_email_ai_read",
		],
	},
# 	"all": [
# 		"ez_accounting.tasks.all"
# 	],
# 	"daily": [
# 		"ez_accounting.tasks.daily"
# 	],
# 	"hourly": [
# 		"ez_accounting.tasks.hourly"
# 	],
# 	"weekly": [
# 		"ez_accounting.tasks.weekly"
# 	],
# 	"monthly": [
# 		"ez_accounting.tasks.monthly"
# 	],
}

# Testing
# -------

# before_tests = "ez_accounting.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "ez_accounting.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "ez_accounting.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "ez_accounting.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["ez_accounting.utils.before_request"]
# after_request = ["ez_accounting.utils.after_request"]

# Job Events
# ----------
# before_job = ["ez_accounting.utils.before_job"]
# after_job = ["ez_accounting.utils.after_job"]

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
# 	"ez_accounting.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

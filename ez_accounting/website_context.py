# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.utils import nowdate

from ez_accounting.api import (
	ADMIN_ROLES,
	forms_company_abbr,
	get_companies,
	get_forms_custom_css,
	get_selected_company,
	is_style_admin,
)
from ez_accounting.permissions import is_page_visible, user_allowed_pages

# Sites with a working ERPNext company behind them: real Customer/Supplier/
# Item/Company data for Express Purchase/Sales/Items, and (once configured
# per-company in Forms Payroll Tax Settings) FICA/Treasurer. Links depending
# on any of this are hidden everywhere else so they don't 500 (no Company at
# all, e.g. your-site.example.com).
GL_LINKS_SITES = {"your-site.example.com"}


def get_website_user_home_page(user: str) -> str:
	"""Root URL destination without turning a guest visit into a 403."""
	return "login" if user == "Guest" else "forms/user/dashboard"


def update_website_context(context):
	"""Swap the default navbar "My Account" link for /forms/user/me on Forms portal pages.

	Scoped to /forms/* only so the rest of this site's navbar (ERPNext, HR, etc.)
	keeps the standard /me profile link.
	"""
	try:
		path = frappe.request.path
	except RuntimeError:
		# No bound request (e.g. called outside an HTTP request lifecycle) — nothing to scope to.
		return

	# The /c/<abbr>/ multi-company URL prefix (see hooks.website_route_rules) —
	# strip it before the /forms/ check; the abbr itself is resolved to a
	# company inside get_selected_company() via ez_accounting.api.request_forms_company().
	prefix_match = re.match(r"^/c/([^/]+)(/forms/.*)$", path)
	if prefix_match:
		path = prefix_match.group(2)
	elif re.match(r"^/c/[^/]+/?$", path):
		path = "/forms/user/dashboard"
	# The user-aware homepage hook resolves an authenticated request for `/`
	# to forms/user/dashboard internally, but request.path remains `/`. Treat
	# that one request as the dashboard here too so it receives the identical
	# company selector, URL prefix, custom style, and permission-gated nav.
	if path == "/" and frappe.session.user != "Guest":
		path = "/forms/user/dashboard"

	if not path.startswith("/forms/"):
		return

	context["post_login"] = [
		{"label": _("My Account"), "url": "/forms/user/me"},
		{"label": _("Log out"), "url": "/logout"},
	]

	# Per-user page access (see forms.permissions / Forms User Access). `None`
	# means unrestricted (no record, or a System Manager) — the common case,
	# unchanged behaviour. A restricted user hitting a page not in their set is
	# intercepted earlier by forms.no_access_renderer (friendly 403 page);
	# here we just trim their nav to what they may open.
	allowed = user_allowed_pages()

	gl = frappe.local.site in GL_LINKS_SITES
	is_admin = bool(ADMIN_ROLES.intersection(frappe.get_roles()))

	def can(link_key, base):
		return base and is_page_visible(link_key, allowed)

	context["show_gl_links"] = gl  # site-level only (also gates dashboard shortcuts)
	context["show_fica_link"] = can("fica", gl)
	context["show_treasurer_link"] = can("treasurer", gl)
	context["show_payrollsummary_link"] = can("payrollsummary", gl)
	context["show_ar_link"] = can("ar", gl)
	context["show_ap_link"] = can("ap", gl)
	context["show_cash_receipts_link"] = can("cash_receipts", gl)
	context["show_cash_disbursements_link"] = can("cash_disbursements", gl)
	context["show_check_register_link"] = can("check_register", gl)
	context["show_customer_ledgers_link"] = can("customer_ledgers", gl)
	context["show_vendor_ledgers_link"] = can("vendor_ledgers", gl)
	context["show_vendor_list_link"] = can("vendor_list", gl)
	context["show_sales_journal_link"] = can("sales_journal", gl)
	context["show_salesreport_link"] = can("salesreport", gl)
	context["show_purchase_journal_link"] = can("purchase_journal", gl)
	context["show_payments_link"] = can("payments", gl)
	context["show_deposits_link"] = can("deposits", gl)
	context["show_files_link"] = can("files", gl)
	context["show_finder_link"] = can("files", gl)
	context["show_manual_link"] = can("manual", gl)
	context["show_vendor_1099_link"] = can("vendor_1099", gl)
	context["show_payroll_link"] = can("payroll", is_admin)
	context["show_payrollsettings_link"] = can("payrollsettings", is_admin)
	context["show_purchase_link"] = can("purchase", gl)
	context["show_sales_link"] = can("sales", gl)
	context["show_invoice_list_link"] = context["show_sales_link"]
	context["show_chart_of_accounts_link"] = gl and frappe.has_permission("Account", "read")
	context["show_items_link"] = can("items", gl)
	context["show_files_link"] = can("files", True)
	context["show_finder_link"] = can("finder", True)
	context["show_deposit_link"] = can("deposit", gl)
	context["show_clearing_link"] = can("clearing", gl)
	context["show_binding_link"] = can("binding", gl)
	context["show_customer_link"] = can("customer", gl)
	context["show_supplier_link"] = can("supplier", gl)
	context["show_employee_link"] = can("employee", gl)
	context["show_reconciliation_link"] = can("reconciliation", gl)
	context["show_printer_link"] = can("printer", gl)
	context["show_printersettings_link"] = can("printersettings", gl)
	context["show_admin_dashboard_link"] = can("admin_dashboard", is_admin)
	context["current_year"] = nowdate()[:4]

	# Company selector (nav) — only shown where there's more than one Company
	# to pick between; single-company sites (or sites with no ERPNext at all)
	# keep working exactly as before, with get_selected_company() falling back
	# to the site's global default company.
	companies = get_companies() if frappe.local.site in GL_LINKS_SITES else []
	context["companies"] = companies
	selected_company = get_selected_company()
	context["selected_company"] = selected_company
	context["show_company_selector"] = len(companies) > 1

	# Multi-company URL prefix (/c/<abbr>/forms/...). Every server-rendered
	# /forms/ link in the nav is built as {{ forms_url_prefix }}/forms/... so
	# navigating stays inside the current company; forms_portal.js rewrites the
	# rest (in-page + JS-built links) client-side using window.FORMS_URL_PREFIX.
	abbr = forms_company_abbr(selected_company) if companies else None
	context["forms_company_abbr"] = abbr
	context["forms_url_prefix"] = ("/c/" + abbr) if abbr else ""

	# Site-wide custom CSS override (see ez_accounting.api.get_forms_custom_css) —
	# rendered by forms_nav.html (included on every /forms/* page) into a
	# <style> tag that loads after the portal's own stylesheet. Not gated to
	# GL_LINKS_SITES like the ERPNext-dependent links above — this is a plain
	# UI setting, unrelated to whether the site has real Company/GL data.
	context["forms_custom_css"] = get_forms_custom_css()
	context["show_style_link"] = is_style_admin()
	# System Managers retain their implicit access. A non-System-Manager sees
	# these sensitive links only when Portal Access explicitly grants the
	# matching /forms/admin page (which also supplies the Forms gate role).
	context["show_permissions_link"] = is_style_admin() or (
		allowed is not None and is_admin and "admin_permissions" in allowed
	)
	context["show_settings_link"] = is_style_admin() or (
		allowed is not None and is_admin and "admin_settings" in allowed
	)

	context["quick_jump_pages"] = _quick_jump_pages(context)


def _quick_jump_pages(context) -> list:
	"""The closed, already-permission-gated set of named /forms/* pages this
	user may reach — rendered once (forms_nav.html) into
	window.FORMS_QUICK_JUMP_PAGES for the Quick Jump palette
	(forms_quick_jump.js) to filter client-side, with no extra page-list
	round trip. Every entry mirrors a real nav link above (same show_*_link
	flag, same URL, same label wording as forms_nav.html) — kept as one
	source of truth here rather than two lists that can drift.

	Deliberately excludes id-scoped detail pages (sales_edit, printer, ...)
	— those need a specific record, not a "jump to a page" destination; reached via the
	Dashboard or Quick Jump's own record search instead.
	"""
	candidates = [
		(True, _("Dashboard"), "/forms/user/dashboard"),
		(context["show_payroll_link"], _("Payroll"), "/forms/user/payroll"),
		(context["show_purchase_link"], _("Purchase"), "/forms/user/purchase"),
		(context["show_sales_link"], _("Sales"), "/forms/user/sales"),
		(context["show_invoice_list_link"], _("Invoices"), "/forms/user/sales#sales-invoice-history"),
		(context["show_chart_of_accounts_link"], _("Chart of Accounts"), "/app/account/view/tree"),
		(context["show_deposit_link"], _("Deposit"), "/forms/user/deposit"),
		(context["show_fica_link"], _("FICA (SS/Medicare)"), "/forms/user/fica"),
		(context["show_treasurer_link"], _("Treasurer (Withholding Tax)"), "/forms/user/treasurer"),
		(context["show_payrollsummary_link"], _("Payroll Summary"), "/forms/user/payrollsummary"),
		(context["show_ar_link"], _("Aged Receivables"), "/forms/user/ar"),
		(context["show_ap_link"], _("Aged Payables"), "/forms/user/ap"),
		(context["show_cash_receipts_link"], _("Cash Receipts Journal"), "/forms/user/cash_receipts"),
		(context["show_cash_disbursements_link"], _("Cash Disbursements Journal"), "/forms/user/cash_disbursements"),
		(context["show_check_register_link"], _("Check Register"), "/forms/user/check_register"),
		(context["show_customer_ledgers_link"], _("Customer Ledgers"), "/forms/user/customer_ledgers"),
		(context["show_vendor_ledgers_link"], _("Vendor Ledgers"), "/forms/user/vendor_ledgers"),
		(context["show_vendor_list_link"], _("Vendor List"), "/forms/user/vendor_list"),
		(context["show_sales_journal_link"], _("Sales Journal"), "/forms/user/sales_journal"),
		(context["show_salesreport_link"], _("Sales Report"), "/forms/user/salesreport"),
		(context["show_purchase_journal_link"], _("Purchase Journal"), "/forms/user/purchase_journal"),
		(context["show_payments_link"], _("Payments"), "/forms/user/payments"),
		(context["show_deposits_link"], _("Deposits"), "/forms/user/deposits"),
		(context["show_finder_link"], _("Document Finder"), "/forms/user/finder"),
		(context["show_manual_link"], _("Procedures Manual"), "/forms/user/manual"),
		(context["show_vendor_1099_link"], _("1099 Vendor Report"), "/forms/user/vendor_1099"),
		(context["show_payrollsettings_link"], _("Payroll Settings"), "/forms/user/payrollsettings"),
		(context["show_customer_link"], _("Customers"), "/forms/user/customer"),
		(context["show_supplier_link"], _("Suppliers"), "/forms/user/supplier"),
		(context["show_employee_link"], _("Employees"), "/forms/user/employee"),
		(context["show_items_link"], _("Items"), "/forms/user/items"),
		(context["show_finder_link"], _("Document Finder"), "/forms/user/finder"),
		(context["show_reconciliation_link"], _("Bank Reconciliation"), "/forms/user/reconciliation"),
		(context["show_clearing_link"], _("Discount Clearing"), "/forms/user/clearing"),
		(context["show_printer_link"], _("Printer"), "/forms/user/printer"),
		(context["show_printersettings_link"], _("Print Settings"), "/forms/user/printersettings"),
		(context["show_binding_link"], _("Default Bindings"), "/forms/user/binding"),
		(context["show_style_link"], _("Custom Styles"), "/forms/user/styles"),
		(context["show_permissions_link"], _("Portal Access"), "/forms/admin/permissions"),
		(context["show_settings_link"], _("App Settings"), "/forms/admin/settings"),
		(True, _("Me"), "/forms/user/me"),
	]
	prefix = context.get("forms_url_prefix") or ""
	return [{"label": label, "url": prefix + url} for shown, label, url in candidates if shown]

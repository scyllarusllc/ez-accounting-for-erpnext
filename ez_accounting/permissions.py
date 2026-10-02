# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

"""Per-user forms-portal page access.

The "navigation half" of restricting a portal user — which header pages they
may open, enforced centrally in ez_accounting.website_context.update_website_context().
This is deliberately NOT the doctype-permission half (view vs create/submit) —
that stays with real Frappe roles, granted separately (see
/forms/admin/permissions' own on-page note and forms-minimal-portal-role).

Opt-in and non-breaking: a user with no `Forms User Access` record is
unrestricted (behaves exactly as before this feature), same as ERPNext User
Permissions. System Managers always bypass.
"""

import re

import frappe
from frappe import _

# The gate role ez_accounting.api.ensure_admin() checks for (besides System Manager).
GATE_ROLE = "Ez Accounting Admin"

# Every restrictable /forms/user/* page, in display order, grouped for the
# admin page's checklist. `label` mirrors the nav labels (forms_nav.html).
# `needs_gate` = the page's own get_context() calls ensure_admin(), so a
# restricted user granted it must also hold GATE_ROLE.
FORMS_PAGE_CATALOG = [
	# key, label, group, needs_gate
	("sales", "Sales", "Transactions", True),
	("purchase", "Purchase", "Transactions", True),
	("deposit", "Deposit", "Transactions", True),
	("payroll", "Payroll", "Transactions", True),
	("customer", "Customers", "People & Records", True),
	("supplier", "Suppliers", "People & Records", True),
	("employee", "Employees", "People & Records", True),
	("items", "Item Accounts", "People & Records", True),
	("files", "Files", "Documents", True),
	("finder", "Document Finder", "Documents", True),
	("manual", "Procedures Manual", "Documents", True),
	("fica", "FICA (SS/Medicare)", "Reports", False),
	("treasurer", "Treasurer (Withholding Tax)", "Reports", False),
	("payrollsummary", "Payroll Summary", "Reports", False),
	("ar", "Aged Receivables", "Reports", False),
	("ap", "Aged Payables", "Reports", False),
	("cash_receipts", "Cash Receipts Journal", "Reports", False),
	("cash_disbursements", "Cash Disbursements Journal", "Reports", False),
	("check_register", "Check Register", "Reports", False),
	("customer_ledgers", "Customer Ledgers", "Reports", False),
	("vendor_ledgers", "Vendor Ledgers", "Reports", False),
	("vendor_list", "Vendor List", "Reports", False),
	("sales_journal", "Sales Journal", "Reports", False),
	("salesreport", "Sales Report", "Reports", True),
	("purchase_journal", "Purchase Journal", "Reports", False),
	("payments", "Payments", "Reports", False),
	("deposits", "Deposits", "Reports", False),
	("vendor_1099", "1099 Vendor Report", "Reports", True),
	("reconciliation", "Bank Reconciliation", "Reconcile & Setup", True),
	("clearing", "Discount Clearing", "Reconcile & Setup", True),
	("binding", "Default Bindings", "Reconcile & Setup", True),
	("payrollsettings", "Payroll Settings", "Reconcile & Setup", True),
	("printer", "Printer", "Reconcile & Setup", True),
	("printersettings", "Print Settings", "Reconcile & Setup", True),
	("admin_dashboard", "Admin Dashboard", "Administration", True),
	("admin_settings", "App Settings", "Administration", True),
	("admin_email", "Email", "Administration", True),
	("admin_finder", "Document Finder", "Administration", True),
	("admin_aiagent", "AI Agent", "Administration", True),
	("admin_ailogs", "AI Logs", "Administration", True),
	("admin_permissions", "Portal Access", "Administration", True),
	("admin_roles", "Role User Permissions", "Administration", True),
]

CATALOG_KEYS = {key for key, _label, _group, _gate in FORMS_PAGE_CATALOG}
GATE_KEYS = {key for key, _label, _group, gate in FORMS_PAGE_CATALOG if gate}

# Always reachable by any logged-in portal user, restricted or not.
UNIVERSAL_PAGES = {"dashboard", "me"}

# A detail page inherits its parent's grant (it has no catalog entry / nav link
# of its own).
DETAIL_PAGE_PARENT = {
	"finder_detail": "finder",
	"finder_private": "finder",
	"sales_edit": "sales",
	"deposit_detail": "deposit",
	"purchase_import": "purchase",
	"customer_profile": "customer",
	"printerhtml": "printer",
}


def route_to_key(path: str) -> str | None:
	"""'/forms/user/sales' -> 'sales'. Detail routes map to their parent key.
	Returns None for anything that isn't a /forms/user/* page (including
	/forms/admin/* — those stay System-Manager-only, never in this model)."""
	if not path:
		return None
	path = path.rstrip("/")
	if path.startswith("/forms/admin/"):
		slug = path[len("/forms/admin/"):].split("/")[0]
		return "admin_" + slug
	prefix = "/forms/user/"
	if not path.startswith(prefix):
		return None
	slug = path[len(prefix):].split("/")[0]
	return DETAIL_PAGE_PARENT.get(slug, slug)


def ensure_explicit_admin_page_access(key: str):
	"""Protect powerful admin APIs even when called outside their web route."""
	user = frappe.session.user
	if user in ("Guest", ""):
		frappe.throw(_("You must be logged in."), frappe.PermissionError)
	roles = frappe.get_roles(user)
	if user == "Administrator" or "System Manager" in roles:
		return
	allowed = user_allowed_pages(user)
	if GATE_ROLE not in roles or allowed is None or key not in allowed:
		frappe.throw(_("You are not permitted to access this administration page."), frappe.PermissionError)


def user_allowed_pages(user: str | None = None) -> set | None:
	"""The set of page keys `user` may open, or None when the user is
	unrestricted (no Forms User Access record, or a System Manager — both
	bypass every check). The returned set already includes UNIVERSAL_PAGES."""
	user = user or frappe.session.user
	if user in ("Administrator", "Guest"):
		return None
	if "System Manager" in frappe.get_roles(user):
		return None
	name = frappe.db.exists("Forms User Access", {"user": user})
	if not name:
		return None
	doc = frappe.get_cached_doc("Forms User Access", name)
	allowed = {row.page for row in doc.pages if row.page in CATALOG_KEYS}
	return allowed | set(UNIVERSAL_PAGES)


def is_page_visible(key: str, allowed: set | None) -> bool:
	"""For the nav flags in website_context: unrestricted (allowed is None) ->
	always visible; restricted -> only if granted."""
	return allowed is None or key in allowed




def forms_access_denied(path: str, user: str | None = None) -> bool:
	"""True when `user` should get the friendly no-access page for this
	/forms/user|admin/ path. Mirrors each page's own ensure_admin /
	ensure_style_admin gate PLUS the per-user Forms User Access restriction —
	so the friendly page can be shown *before* the page module runs (see
	ez_accounting.no_access_renderer). Best-effort UX layer; the pages' own
	ensure_*() calls stay the real security boundary.
	"""
	if not path:
		return False
	p = "/" + path.strip("/ ")
	# The /c/<abbr>/ multi-company URL prefix (see hooks.website_route_rules) —
	# the access check keys off the /forms/user|admin/ shape, so strip it first.
	prefix_match = re.match(r"^/c/[^/]+(/forms/.*)$", p)
	if prefix_match:
		p = prefix_match.group(1)
	elif re.match(r"^/c/[^/]+/?$", p):
		p = "/forms/user/dashboard"
	if not (p.startswith("/forms/user/") or p.startswith("/forms/admin/")):
		return False

	user = user or frappe.session.user
	if user in ("Guest", "Administrator"):
		return False  # Guest -> normal login redirect; Administrator -> everything
	roles = frappe.get_roles(user)
	if "System Manager" in roles:
		return False

	# 1. per-user page restriction
	allowed = user_allowed_pages(user)
	key = route_to_key(p)
	if allowed is not None and key is not None and key not in allowed:
		return True

	# 2. the pages' own admin gates (for users with no restriction record, or
	#    a record that grants only non-gated report pages)
	if p.startswith("/forms/admin/"):
		if key in ("admin_settings", "admin_permissions", "admin_roles", "admin_email", "admin_finder", "admin_aiagent", "admin_ailogs") and (
			allowed is None or key not in allowed
		):
			return True
		return GATE_ROLE not in roles
	if key == "styles":  # /forms/user/styles -> System Manager only
		return True
	if key in GATE_KEYS:
		return GATE_ROLE not in roles
	return False

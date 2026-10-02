# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _

from ez_accounting.permissions import CATALOG_KEYS, FORMS_PAGE_CATALOG, ensure_explicit_admin_page_access

no_cache = 1


def get_context(context):
	"""Portal Access console — per forms user, which header pages they may
	open + their company scope. System Managers always have access; another
	Forms administrator must be explicitly granted this admin page.

	Deliberately does NOT manage doctype-level permissions (view vs
	create/submit) — those stay with real Frappe roles, granted via Role
	Permission Manager. This page only controls navigation/page access + the
	"Ez Accounting Admin" gate role + the Company User Permission (all handled by
	the Forms User Access record's own controller).
	"""
	ensure_explicit_admin_page_access("admin_permissions")

	context.body_class = "forms-portal-dark"
	context.nav_active = "permissions"
	context.title = _("Portal Access")
	return context


def _catalog_grouped():
	groups = {}
	for key, label, group, needs_gate in FORMS_PAGE_CATALOG:
		groups.setdefault(group, []).append({"key": key, "label": label, "needs_gate": bool(needs_gate)})
	# preserve first-seen group order
	seen = []
	for _key, _label, group, _gate in FORMS_PAGE_CATALOG:
		if group not in seen:
			seen.append(group)
	return [{"group": g, "pages": groups[g]} for g in seen]


@frappe.whitelist()
def get_access_overview():
	ensure_explicit_admin_page_access("admin_permissions")

	access_by_user = {}
	for row in frappe.get_all("Forms User Access", fields=["name", "user"], limit_page_length=0):
		doc = frappe.get_doc("Forms User Access", row.name)
		access_by_user[row.user] = {
			"companies": [c.company for c in doc.companies if c.company],
			"pages": [p.page for p in doc.pages if p.page in CATALOG_KEYS],
		}

	users = []
	for u in frappe.get_all(
		"User",
		filters={"enabled": 1, "name": ["not in", ["Administrator", "Guest"]]},
		fields=["name", "full_name", "user_type"],
		order_by="full_name",
		limit_page_length=0,
	):
		roles = frappe.get_roles(u.name)
		is_sm = "System Manager" in roles
		acc = access_by_user.get(u.name)
		users.append(
			{
				"user": u.name,
				"full_name": u.full_name or u.name,
				"user_type": u.user_type,
				"is_system_manager": is_sm,
				"managed": bool(acc) and not is_sm,
				"allowed_pages": (acc or {}).get("pages", []),
				"companies": (acc or {}).get("companies", []),
			}
		)

	return {
		"catalog": _catalog_grouped(),
		"users": users,
		"companies": frappe.get_all("Company", fields=["name"], order_by="name", limit_page_length=0),
	}


def _check_target(user: str):
	if not frappe.db.exists("User", user) or user in ("Administrator", "Guest"):
		frappe.throw(_("{0} is not a user.").format(user))
	if "System Manager" in frappe.get_roles(user):
		frappe.throw(_("{0} is a System Manager — always has full access, nothing to manage here.").format(user))


@frappe.whitelist()
def save_user_access(user: str, pages="[]", companies="[]"):
	"""Upsert the user's Forms User Access record. Zero pages = the user is
	still managed but locked to Dashboard / Me only (and the "Ez Accounting Admin"
	gate role is removed if this console added it). Zero companies = no company
	restriction. To stop managing a user entirely, use remove_management()."""
	ensure_explicit_admin_page_access("admin_permissions")
	_check_target(user)

	raw = json.loads(pages) if isinstance(pages, str) else (pages or [])
	bad = set(raw) - CATALOG_KEYS
	if bad:
		frappe.throw(_("Unknown page(s): {0}").format(", ".join(sorted(bad))))
	page_keys = [k for k in raw if k in CATALOG_KEYS]

	raw_companies = json.loads(companies) if isinstance(companies, str) else (companies or [])
	company_names, seen = [], set()
	for c in raw_companies:
		c = (c or "").strip()
		if c and c not in seen:
			if not frappe.db.exists("Company", c):
				frappe.throw(_("Unknown company: {0}").format(c))
			seen.add(c)
			company_names.append(c)

	existing = frappe.db.exists("Forms User Access", {"user": user})
	doc = frappe.get_doc("Forms User Access", existing) if existing else frappe.new_doc("Forms User Access")
	doc.user = user
	doc.set("pages", [{"page": k} for k in page_keys])
	doc.set("companies", [{"company": c} for c in company_names])
	doc.save(ignore_permissions=True)

	return {
		"managed": True,
		"allowed_pages": [p.page for p in doc.pages],
		"companies": [c.company for c in doc.companies],
	}


@frappe.whitelist()
def remove_management(user: str):
	"""Delete the user's Forms User Access record — they revert to whatever
	their Frappe roles allow (unrestricted within the portal)."""
	ensure_explicit_admin_page_access("admin_permissions")
	_check_target(user)
	existing = frappe.db.exists("Forms User Access", {"user": user})
	if existing:
		frappe.delete_doc("Forms User Access", existing, ignore_permissions=True)
	return {"managed": False}

"""Company boundary for the Forms file catalog, including Desk access."""

import frappe

from ez_accounting.api import permitted_docs


def allowed_companies(user=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return None
	if user == "Guest":
		return []
	if user != frappe.session.user:
		from frappe.permissions import get_user_permissions
		rules = get_user_permissions(user).get("Company", [])
		return [rule.doc for rule in rules if rule.doc]
	# A missing Company permission is not an all-company grant for this catalog.
	return permitted_docs("Company") or []


def permission_query_conditions(user=None):
	companies = allowed_companies(user)
	if companies is None:
		return None
	if not companies:
		return "1=0"
	return "`tabForms File Record`.`company` in (" + ", ".join(
		frappe.db.escape(company) for company in companies
	) + ")"


def has_permission(doc, ptype=None, user=None, debug=False):
	companies = allowed_companies(user)
	return companies is None or doc.company in companies

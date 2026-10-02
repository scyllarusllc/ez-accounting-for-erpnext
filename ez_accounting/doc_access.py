"""Owner-level access rule for Forms Company Document.

A document flagged `is_private` is visible/manageable only by its owner; every
other document stays company-shared. Enforced in the portal code paths
(files/finder) via visibility_filters()/assert_can_access(), and for Desk, the
REST API and private-file downloads via the doctype hooks below.
"""

import frappe
from frappe import _

DOCTYPE = "Forms Company Document"


def visibility_conditions(user=None, alias=None):
	"""SQL condition hiding other users' private documents."""
	user = user or frappe.session.user
	table = alias or f"`tab{DOCTYPE}`"
	return f"({table}.`is_private` = 0 or {table}.`owner` = {frappe.db.escape(user)})"


def permission_query_conditions(user=None):
	return visibility_conditions(user)


def has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	return not doc.get("is_private") or doc.owner == user


def hidden_names(user=None) -> list[str]:
	"""Names of other users' private documents (for `name not in` filters --
	works alongside a caller's own or_filters and keeps pagination exact)."""
	user = user or frappe.session.user
	return frappe.get_all(
		DOCTYPE, filters={"is_private": 1, "owner": ["!=", user]}, pluck="name", limit_page_length=0
	)


def add_visibility(filters: list, user=None) -> list:
	"""get_all() filter list plus a clause excluding other users' private docs."""
	hidden = hidden_names(user)
	return filters + [["name", "not in", hidden]] if hidden else filters


def assert_can_access(doc):
	if not has_permission(doc):
		frappe.throw(_("This document is private to its owner."), frappe.PermissionError)

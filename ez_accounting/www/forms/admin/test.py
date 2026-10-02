"""Scratch placeholder page requested via the mini Martin chat widget --
just confirms the widget can add a page. Nothing here backs onto real data."""

import frappe
from frappe import _

no_cache = 1


def get_context(context):
	if frappe.session.user in ("Guest", ""):
		frappe.throw(_("You must be logged in."), frappe.PermissionError)
	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(_("You are not permitted to access this administration page."), frappe.PermissionError)

	context.body_class = "forms-portal-dark"
	context.nav_active = "test"
	context.title = _("Test")
	return context

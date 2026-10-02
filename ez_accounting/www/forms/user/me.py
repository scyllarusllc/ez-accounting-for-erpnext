# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _

from ez_accounting.api import ensure_logged_in

no_cache = 1


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "me"
	context.title = _("Forms — My Account")

	# NOTE: don't name this context key "user" — frappe.website.page_renderers
	# .TemplatePage.set_user_info() runs after get_context() and unconditionally
	# overwrites context["user"] with the session user's plain login string.
	account = frappe.get_doc("User", frappe.session.user)
	context.account = account
	context.initials = "".join(part[0].upper() for part in (account.full_name or account.email).split()[:2])

	all_roles = sorted(r for r in frappe.get_roles(frappe.session.user) if r != "All")
	visible_role_count = 8
	context.roles = all_roles[:visible_role_count]
	context.hidden_roles = all_roles[visible_role_count:]

	# "My Style" — every user's own personal override of the Forms portal's
	# System Default (see ez_accounting.api.get_forms_custom_css()/set_my_forms_style()),
	# not just System Managers (who alone can author/edit a style's actual CSS
	# on /forms/user/styles — picking one someone else wrote carries none of
	# that risk). Only shown once at least one style exists to pick from.
	context.forms_styles = frappe.get_all("Forms Style", pluck="style_name", order_by="style_name asc")
	context.my_forms_style = frappe.defaults.get_user_default("forms_style") or ""

	return context

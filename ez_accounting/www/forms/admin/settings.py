# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

"""The Forms app's own site-wide settings — System Manager only
(ensure_style_admin(), the same gate /forms/admin/permissions and
/forms/user/styles' own style-editing controls already use, despite the
name — see that function's own docstring for why "style admin" means
System Manager specifically, not the "Ez Accounting Admin" gate role every
other /forms/admin/* page accepts).

Currently just the one real site-wide setting this app has — System
Default Style (Forms Style Settings.default_style) — but built as a
general settings hub on purpose: that setting used to live only inside
/forms/user/styles, a page that's really about *authoring* style CSS, not
choosing site-wide settings, so "which style is the default" was buried
inside a workspace that has nothing else to do with "the app's settings."
Saving still goes through that same page's own set_default_style()
(this module doesn't duplicate the write) — this page is just a second,
more fitting front door onto it, ready to gain more settings later
without each one needing its own bespoke home.
"""

import frappe
from frappe import _

from ez_accounting.permissions import ensure_explicit_admin_page_access

no_cache = 1


def get_context(context):
	ensure_explicit_admin_page_access("admin_settings")

	context.body_class = "forms-portal-dark"
	context.nav_active = "settings"
	context.title = _("App Settings")
	return context


@frappe.whitelist()
def get_settings():
	ensure_explicit_admin_page_access("admin_settings")

	styles = frappe.get_all("Forms Style", fields=["name", "style_name"], order_by="style_name asc")
	default_style = frappe.db.get_single_value("Forms Style Settings", "default_style")

	return {"styles": styles, "default_style": default_style}

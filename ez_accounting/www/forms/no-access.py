# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _

no_cache = 1


def get_context(context):
	context.body_class = "forms-portal-dark"
	context.nav_active = ""
	context.title = _("No Access")
	return context

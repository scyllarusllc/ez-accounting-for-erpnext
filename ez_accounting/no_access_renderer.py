# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

"""Custom website page renderer — intercepts a /forms/user|admin/ request the
current user isn't allowed and renders the friendly `www/forms/no-access.html`
page (403) instead of Frappe's generic "Not Permitted" screen. Registered as a
`page_renderer` hook, so it runs in PathResolver *before* the normal
TemplatePage (i.e. before the page module's own get_context / ensure_admin).

See ez_accounting.permissions.forms_access_denied for the check.
"""

import frappe
from frappe.website.page_renderers.template_page import TemplatePage

from ez_accounting.permissions import forms_access_denied


class FormsNoAccessPage(TemplatePage):
	def __init__(self, path=None, http_status_code=None):
		# Instantiated for *every* website request (custom renderers are tried
		# first) — keep this cheap; real TemplatePage init is deferred to
		# render(), which only runs when can_render() said yes.
		self._requested = path or (
			frappe.local.request.path if getattr(frappe.local, "request", None) else ""
		)

	def can_render(self):
		try:
			return forms_access_denied(self._requested)
		except Exception:
			return False

	def render(self):
		super().__init__(path="forms/no-access", http_status_code=403)
		return super().render()

"""Read-only detail page for a document returned by Document Finder."""

from pathlib import PurePosixPath

import frappe
from frappe import _

from ez_accounting.api import ensure_admin, get_companies
from ez_accounting.doc_access import assert_can_access

no_cache = 1


def get_context(context):
	ensure_admin()
	context.body_class = "forms-portal-dark"
	context.nav_active = "finder"
	context.title = _("Document Detail")
	context.document = None
	context.not_found = False
	context.forbidden = False

	name = (frappe.form_dict.get("id") or "").strip()
	if not name or not frappe.db.exists("Forms Company Document", name):
		context.not_found = True
		return context

	doc = frappe.get_doc("Forms Company Document", name)
	available_companies = {row.name for row in get_companies()}
	if doc.company not in available_companies:
		context.forbidden = True
		return context

	try:
		assert_can_access(doc)
	except frappe.PermissionError:
		context.forbidden = True
		return context

	context.document = doc
	context.title = doc.title or doc.name
	context.file_is_safe = bool(doc.file and str(doc.file).startswith(("/files/", "/private/files/")))
	context.file_extension = PurePosixPath(doc.file_name or doc.file or "").suffix.lower()
	return context

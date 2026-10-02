# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _

from ez_accounting.api import ensure_admin, get_selected_company

no_cache = 1


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "printer"
	context.title = _("Printer")
	context.company = get_selected_company()
	return context


@frappe.whitelist()
def detect_doctype(name: str):
	"""Guess which doctype a raw document ID belongs to — the whole point of
	this page is "just provide an ID", with no doctype picker up front.
	Rather than parsing naming-series prefixes (brittle — every doctype's own
	autoname pattern would need hardcoding, and would silently drift as new
	ones get added), this just checks existence against every doctype that
	actually has at least one enabled Print Format — self-maintaining (a
	newly-added Print Format for some doctype makes it detectable here
	automatically, no code change needed) and small enough (currently ~19
	doctypes on this site) that a handful of exists() checks per lookup is
	cheap. Returns every doctype the ID actually exists in — usually one,
	occasionally more if a coincidentally-identical name exists in two
	unrelated doctypes (the client shows a picker in that case).
	"""
	ensure_admin()

	name = (name or "").strip()
	if not name:
		return []

	candidates = frappe.get_all("Print Format", filters={"disabled": 0}, pluck="doc_type")
	candidates = sorted({c for c in candidates if c})

	return [dt for dt in candidates if frappe.db.exists(dt, name)]


@frappe.whitelist()
def get_print_formats(doctype: str):
	"""Enabled Print Formats for one doctype, standard ones first (usually
	the more "official"/complete layouts) then alphabetical.
	"""
	ensure_admin()

	if not doctype:
		frappe.throw(_("Doctype is required."))

	return frappe.get_all(
		"Print Format",
		filters={"doc_type": doctype, "disabled": 0},
		fields=["name", "standard"],
		order_by="standard desc, name asc",
	)

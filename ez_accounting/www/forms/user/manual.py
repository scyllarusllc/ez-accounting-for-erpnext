# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint

from ez_accounting.api import ensure_admin, get_selected_company

no_cache = 1


def get_context(context):
	ensure_admin()
	context.body_class = "forms-portal-dark"
	context.nav_active = "manual"
	context.title = _("Procedures Manual")
	context.company = get_selected_company()
	return context


def _company():
	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))
	return company


@frappe.whitelist()
def get_entries():
	ensure_admin()
	return frappe.get_all(
		"Forms Manual Entry",
		filters={"company": _company()},
		fields=["name", "category", "question", "answer", "sort_order", "modified", "modified_by"],
		order_by="sort_order asc, category asc, question asc",
		limit_page_length=0,
	)


@frappe.whitelist()
def save_entry(name: str = "", category: str = "", question: str = "", answer: str = "", sort_order: int = 0):
	ensure_admin()
	company = _company()
	question = (question or "").strip()
	answer = (answer or "").strip()
	if not question:
		frappe.throw(_("Question or procedure title is required."))
	if not answer:
		frappe.throw(_("Answer or instructions are required."))

	if name:
		doc = frappe.get_doc("Forms Manual Entry", name)
		if doc.company != company:
			frappe.throw(_("This manual entry belongs to another company."), frappe.PermissionError)
	else:
		doc = frappe.new_doc("Forms Manual Entry")
		doc.company = company
	doc.category = (category or "").strip()
	doc.question = question
	doc.answer = answer
	doc.sort_order = cint(sort_order)
	doc.save(ignore_permissions=True)
	return {"name": doc.name}


@frappe.whitelist()
def delete_entry(name: str):
	ensure_admin()
	doc = frappe.get_doc("Forms Manual Entry", name)
	if doc.company != _company():
		frappe.throw(_("This manual entry belongs to another company."), frappe.PermissionError)
	frappe.delete_doc("Forms Manual Entry", name, ignore_permissions=True)
	return {"deleted": name}

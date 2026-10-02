"""Private Document Finder: same as /forms/user/finder, but limited to the
Forms Company Documents the logged-in user created (owner = session user)."""

import frappe
from frappe import _

from ez_accounting.www.forms.user.finder import search_documents
from ez_accounting.www.forms.user.files import CATEGORIES
from ez_accounting.api import ensure_admin

no_cache = 1


def get_context(context):
    ensure_admin()
    context.body_class = "forms-portal-dark"
    context.nav_active = "finder_private"
    context.title = _("My Private Documents")
    context.categories = CATEGORIES
    return context


@frappe.whitelist(methods=["GET"])
def search(q="", company="", category="", document_from="", document_to="", expiry_from="", expiry_to="", page=0):
    return search_documents(q, company, category, document_from, document_to, expiry_from, expiry_to, page,
                            owner_only=True)

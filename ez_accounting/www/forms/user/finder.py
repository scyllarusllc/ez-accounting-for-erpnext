"""Search existing Forms Company Document records across authorized companies."""

from datetime import date
import re

import frappe
from frappe import _

from ez_accounting.api import ensure_admin, get_companies
from ez_accounting.doc_access import add_visibility
from ez_accounting.www.forms.user.files import CATEGORIES

no_cache = 1


def get_context(context):
    ensure_admin()
    context.body_class = "forms-portal-dark"
    context.nav_active = "finder"
    context.title = _("Document Finder")
    context.categories = CATEGORIES
    return context


def _date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        frappe.throw(_("Invalid date"))


def _match_excerpt(row, keyword):
    """Return only a short matching passage, never the full extracted text."""
    for field, label in (("title", "Title"), ("tags", "Tags"),
                         ("description", "Description"), ("file_name", "File name"),
                         ("content", "Extracted content")):
        value = str(row.get(field) or "")
        match = re.search(re.escape(keyword), value, re.IGNORECASE)
        if match:
            start = max(0, match.start() - 65)
            end = min(len(value), match.end() + 100)
            excerpt = ("…" if start else "") + value[start:end].replace("\n", " ") + ("…" if end < len(value) else "")
            return label, excerpt
    return "", ""


@frappe.whitelist(methods=["GET"])
def search(q="", company="", category="", document_from="", document_to="", expiry_from="", expiry_to="", page=0):
    return search_documents(q, company, category, document_from, document_to, expiry_from, expiry_to, page)


def search_documents(q="", company="", category="", document_from="", document_to="", expiry_from="",
                     expiry_to="", page=0, owner_only=False):
    ensure_admin()
    available = [row.name for row in get_companies()]
    if not available:
        return {"rows": [], "has_more": False, "companies": []}
    if company and company not in available:
        frappe.throw(_("Company is not available to this user"), frappe.PermissionError)
    if category and category not in CATEGORIES:
        frappe.throw(_("Invalid category"))
    q = (q or "").strip()
    if len(q) > 200:
        frappe.throw(_("Search text is too long"))
    try:
        page = int(page)
    except (TypeError, ValueError):
        frappe.throw(_("Invalid page"))
    if page < 0 or page > 1000:
        frappe.throw(_("Invalid page"))
    filters = [["company", "in", [company] if company else available]]
    if owner_only:
        filters.append(["owner", "=", frappe.session.user])
    filters = add_visibility(filters)
    if category:
        filters.append(["category", "=", category])
    for field, operator, value in (
        ("document_date", ">=", document_from),
        ("document_date", "<=", document_to),
        ("expiry_date", ">=", expiry_from),
        ("expiry_date", "<=", expiry_to),
    ):
        parsed = _date(value)
        if parsed:
            filters.append([field, operator, parsed])
    or_filters = None
    if q:
        needle = "%" + q + "%"
        or_filters = [[field, "like", needle] for field in
            ("title", "tags", "description", "file_name", "content")]
    rows = frappe.get_all(
        "Forms Company Document", filters=filters, or_filters=or_filters,
        fields=["name", "company", "title", "category", "document_date", "expiry_date",
                "file", "file_name", "description", "tags", "ai_read_at", "creation", "is_private"]
                + (["content"] if q else []),
        order_by="creation desc, name desc", start=page * 25, limit_page_length=26,
    )
    for row in rows:
        if q:
            row["match_field"], row["match_excerpt"] = _match_excerpt(row, q)
            row.pop("content", None)
    return {"rows": rows[:25], "has_more": len(rows) > 25,
            "companies": available if page == 0 else []}

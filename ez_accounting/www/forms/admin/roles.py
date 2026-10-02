"""Reusable Forms portal page and company access collections by role."""
import json
import frappe
from frappe import _
from ez_accounting.permissions import CATALOG_KEYS, FORMS_PAGE_CATALOG, ensure_explicit_admin_page_access
from ez_accounting.www.forms.admin.permissions import _check_target, save_user_access
no_cache = 1

def get_context(context):
    ensure_explicit_admin_page_access("admin_roles")
    context.body_class = "forms-portal-dark"
    context.nav_active = "roles"
    context.title = _("Role Forms Access")
    return context

def _ensure_access():
    ensure_explicit_admin_page_access("admin_roles")

def _catalog_grouped():
    groups, order = {}, []
    for key, label, group, needs_gate in FORMS_PAGE_CATALOG:
        if group not in groups:
            groups[group] = []
            order.append(group)
        groups[group].append({"key": key, "label": label, "needs_gate": bool(needs_gate)})
    return [{"group": group, "pages": groups[group]} for group in order]

def _members(role: str) -> list[dict]:
    return frappe.db.sql("""
        SELECT DISTINCT u.name, u.full_name FROM `tabUser` u
        INNER JOIN `tabHas Role` hr ON hr.parent = u.name AND hr.parenttype = 'User'
        WHERE hr.role = %s AND u.enabled = 1 AND u.name NOT IN ('Guest', 'Administrator')
        ORDER BY COALESCE(NULLIF(u.full_name, ''), u.name), u.name
    """, role, as_dict=True)

@frappe.whitelist()
def get_roles():
    _ensure_access()
    roles = frappe.get_all("Role", filters={"disabled": 0}, pluck="name", order_by="name")
    configured = set(frappe.get_all("Forms Role Permission Collection", pluck="name", limit_page_length=0))
    return {
        "roles": [{"role": role, "configured": role in configured} for role in roles],
        "catalog": _catalog_grouped(),
        "companies": frappe.get_all("Company", fields=["name"], order_by="name", limit_page_length=0),
    }

@frappe.whitelist()
def get_collection(role: str):
    _ensure_access()
    if not frappe.db.exists("Role", role):
        frappe.throw(_("Role does not exist."))
    doc = frappe.get_doc("Forms Role Permission Collection", role) if frappe.db.exists("Forms Role Permission Collection", role) else None
    return {
        "role": role,
        "pages": [] if not doc else [row.page for row in doc.pages if row.page in CATALOG_KEYS],
        "companies": [] if not doc else [row.company for row in doc.companies if row.company],
        "members": _members(role),
    }

def _json_list(value) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = []
    return value if isinstance(value, list) else []

def _validate_pages(pages) -> list[str]:
    pages = _json_list(pages)
    bad = set(pages) - CATALOG_KEYS
    if bad:
        frappe.throw(_("Unknown page(s): {0}").format(", ".join(sorted(bad))))
    return list(dict.fromkeys(key for key in pages if key in CATALOG_KEYS))

def _validate_companies(companies) -> list[str]:
    cleaned = []
    for company in _json_list(companies):
        company = str(company or "").strip()
        if company and company not in cleaned:
            if not frappe.db.exists("Company", company):
                frappe.throw(_("Unknown company: {0}").format(company))
            cleaned.append(company)
    return cleaned

@frappe.whitelist()
def save_collection(role: str, pages=None, companies=None):
    _ensure_access()
    if not frappe.db.exists("Role", role):
        frappe.throw(_("Role does not exist."))
    page_keys = _validate_pages(pages)
    company_names = _validate_companies(companies)
    if frappe.db.exists("Forms Role Permission Collection", role):
        doc = frappe.get_doc("Forms Role Permission Collection", role)
    else:
        doc = frappe.new_doc("Forms Role Permission Collection")
        doc.role = role
    doc.set("pages", [{"page": key} for key in page_keys])
    doc.set("companies", [{"company": company} for company in company_names])
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"role": role, "pages": page_keys, "companies": company_names}

@frappe.whitelist()
def apply_collection(role: str, user: str = ""):
    _ensure_access()
    if not frappe.db.exists("Forms Role Permission Collection", role):
        frappe.throw(_("Save an access collection for this role first."))
    doc = frappe.get_doc("Forms Role Permission Collection", role)
    members = _members(role)
    member_names = {row.name for row in members}
    if user:
        if user not in member_names:
            frappe.throw(_("The selected user does not have this role."))
        targets = [user]
    else:
        targets = sorted(member_names)
    if not targets:
        frappe.throw(_("No manageable enabled users have this role."))
    pages = [row.page for row in doc.pages if row.page in CATALOG_KEYS]
    companies = [row.company for row in doc.companies if row.company]
    for target in targets:
        _check_target(target)
        save_user_access(target, pages, companies)
    frappe.db.commit()
    return {"users": len(targets), "pages": len(pages), "companies": len(companies)}

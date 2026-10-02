# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _

from ez_accounting.api import apply_permitted_filter, ensure_logged_in, get_selected_company

no_cache = 1


def _contacts_for(supplier_names: list[str]) -> dict[str, dict]:
	"""{supplier: {name, phone}} — the primary contact for each supplier
	(Supplier.supplier_primary_contact when set, otherwise the first Contact
	linked to the supplier via Dynamic Link). Phone: the contact's primary
	Contact Phone, then its `phone`, then its `mobile_no`.
	"""
	if not supplier_names:
		return {}

	primary = {
		r.name: r.supplier_primary_contact
		for r in frappe.get_all(
			"Supplier",
			filters={"name": ["in", supplier_names]},
			fields=["name", "supplier_primary_contact"],
		)
		if r.supplier_primary_contact
	}

	# For suppliers with no primary contact, find the first linked Contact.
	need_link = [s for s in supplier_names if s not in primary]
	if need_link:
		links = frappe.get_all(
			"Dynamic Link",
			filters={"parenttype": "Contact", "link_doctype": "Supplier", "link_name": ["in", need_link]},
			fields=["parent", "link_name"],
			order_by="creation asc",
		)
		for link in links:
			primary.setdefault(link.link_name, link.parent)

	contact_names = sorted(set(primary.values()))
	if not contact_names:
		return {}

	contacts = {
		c.name: c
		for c in frappe.get_all(
			"Contact",
			filters={"name": ["in", contact_names]},
			fields=["name", "first_name", "last_name", "phone", "mobile_no"],
		)
	}
	primary_phone = {
		p.parent: p.phone
		for p in frappe.get_all(
			"Contact Phone",
			filters={"parent": ["in", contact_names], "is_primary_phone": 1},
			fields=["parent", "phone"],
		)
	}

	out = {}
	for supplier, contact_name in primary.items():
		c = contacts.get(contact_name)
		if not c:
			continue
		full = " ".join(x for x in [c.first_name, c.last_name] if x).strip()
		out[supplier] = {
			"name": full,
			"phone": primary_phone.get(contact_name) or c.phone or c.mobile_no or "",
		}
	return out


def _build_vendor_list() -> list[dict]:
	filters = apply_permitted_filter([["disabled", "=", 0]], "Supplier", "name")
	suppliers = frappe.get_all(
		"Supplier",
		filters=filters,
		fields=["name", "supplier_name", "tax_id", "mobile_no", "supplier_group"],
		order_by="name asc",
		limit_page_length=0,
	)
	contacts = _contacts_for([s.name for s in suppliers])

	rows = []
	for s in suppliers:
		c = contacts.get(s.name) or {}
		rows.append(
			{
				"vendor_id": s.name,
				"vendor": s.supplier_name or s.name,
				"group": s.supplier_group or "",
				"contact": c.get("name", ""),
				"phone": c.get("phone") or s.mobile_no or "",
				"tax_id": s.tax_id or "",
			}
		)
	return rows


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "vendor_list"
	context.title = _("Vendor List")
	context.company = get_selected_company()
	context.vendors = _build_vendor_list()
	return context


@frappe.whitelist()
def download_excel():
	ensure_logged_in()

	from frappe.utils.xlsxutils import build_xlsx_response

	rows = _build_vendor_list()
	header = [_("Vendor ID"), _("Vendor"), _("Group"), _("Contact"), _("Telephone 1"), _("Tax Id No")]
	out = [header]
	for r in rows:
		out.append([r["vendor_id"], r["vendor"], r["group"], r["contact"], r["phone"], r["tax_id"]])

	build_xlsx_response(out, "Vendor List")

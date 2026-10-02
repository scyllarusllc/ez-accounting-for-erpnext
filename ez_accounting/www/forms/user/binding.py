# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _

from ez_accounting.api import (
	ensure_admin,
	get_selected_company,
	upsert_party_default,
)

no_cache = 1

# The only fields this page's inline edit is allowed to touch — kept as an
# explicit allowlist (rather than trusting whatever fieldname the client
# sends) since update_party_default() does a plain setattr() onto the doc,
# same reasoning as items.py's ACCOUNT_FIELDS.
EDITABLE_FIELDS = {"item", "rate", "mode_of_payment", "reference_no", "remarks"}


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "binding"
	context.title = _("Default Bindings")
	context.company = get_selected_company()
	return context


@frappe.whitelist()
def get_party_defaults():
	"""Every remembered Customer/Supplier default for the selected company,
	with the party's display name and the item's display name resolved for
	the table — Forms Party Default itself only stores the docnames.
	"""
	ensure_admin()

	company = get_selected_company()
	if not company:
		return []

	rows = frappe.get_all(
		"Forms Party Default",
		filters={"company": company},
		fields=["name", "party_type", "party", "item", "rate", "mode_of_payment", "reference_no", "remarks"],
		order_by="party_type asc, party asc",
	)
	if not rows:
		return rows

	customer_names = {r.party: True for r in rows if r.party_type == "Customer"}
	supplier_names = {r.party: True for r in rows if r.party_type == "Supplier"}
	item_codes = {r.item: True for r in rows if r.item}

	customer_labels = (
		{c.name: c.customer_name for c in frappe.get_all("Customer", filters={"name": ["in", list(customer_names)]}, fields=["name", "customer_name"])}
		if customer_names
		else {}
	)
	supplier_labels = (
		{s.name: s.supplier_name for s in frappe.get_all("Supplier", filters={"name": ["in", list(supplier_names)]}, fields=["name", "supplier_name"])}
		if supplier_names
		else {}
	)
	item_labels = (
		{i.name: i.item_name for i in frappe.get_all("Item", filters={"name": ["in", list(item_codes)]}, fields=["name", "item_name"])}
		if item_codes
		else {}
	)

	for row in rows:
		labels = customer_labels if row.party_type == "Customer" else supplier_labels
		row["party_name"] = labels.get(row.party, row.party)
		row["item_name"] = item_labels.get(row.item, row.item)

	return rows


@frappe.whitelist()
def get_customers():
	ensure_admin()
	return frappe.get_all(
		"Customer",
		filters={"disabled": 0},
		fields=["name", "customer_name"],
		order_by="customer_name asc",
	)


@frappe.whitelist()
def get_suppliers():
	ensure_admin()
	return frappe.get_all(
		"Supplier",
		filters={"disabled": 0},
		fields=["name", "supplier_name"],
		order_by="supplier_name asc",
	)


@frappe.whitelist()
def get_items():
	"""Active items usable on either side (Sales or Purchase) — the "Add
	binding" form doesn't know yet which one a given item will be used for.
	"""
	ensure_admin()
	return frappe.get_all(
		"Item",
		filters={"disabled": 0},
		fields=["name", "item_name"],
		order_by="item_name asc",
	)


@frappe.whitelist()
def get_modes_of_payment():
	ensure_admin()
	return frappe.get_all(
		"Mode of Payment",
		filters={"enabled": 1},
		fields=["name"],
		order_by="name asc",
	)


@frappe.whitelist()
def update_party_default(name: str, field: str, value: str = ""):
	"""Inline edit of one field on an existing binding."""
	ensure_admin()

	if field not in EDITABLE_FIELDS:
		frappe.throw(_("Invalid field: {0}").format(field))

	company = get_selected_company()
	doc = frappe.get_doc("Forms Party Default", name)
	if doc.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(name, company))

	if field == "item" and not value:
		frappe.throw(_("Item is required."))

	doc.set(field, value or None)
	doc.save()

	return {"name": doc.name, "field": field, "value": doc.get(field)}


@frappe.whitelist()
def create_party_default(
	party_type: str,
	party: str,
	item: str,
	rate=None,
	mode_of_payment: str = "",
	reference_no: str = "",
	remarks: str = "",
):
	"""Manually add (or overwrite) a binding from this page — routed through
	the same upsert_party_default() Express Sales/Purchase call after a real
	transaction, so there's one code path for how a binding gets written
	either way.
	"""
	ensure_admin()

	if party_type not in ("Customer", "Supplier"):
		frappe.throw(_("Party Type must be Customer or Supplier."))
	if not party:
		frappe.throw(_("{0} is required.").format(party_type))
	if not item:
		frappe.throw(_("Item is required."))

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	if not frappe.db.exists(party_type, party):
		frappe.throw(_("{0} is not a valid {1}.").format(party, party_type))
	if not frappe.db.exists("Item", item):
		frappe.throw(_("{0} is not a valid Item.").format(item))

	doc = upsert_party_default(
		party_type,
		party,
		company,
		item,
		rate,
		mode_of_payment or None,
		reference_no.strip() if reference_no and reference_no.strip() else None,
		remarks.strip() if remarks and remarks.strip() else None,
	)
	return {"name": doc.name}


@frappe.whitelist()
def delete_party_default(name: str):
	ensure_admin()

	company = get_selected_company()
	doc = frappe.get_doc("Forms Party Default", name)
	if doc.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(name, company))

	doc.delete()
	return {"deleted": name}

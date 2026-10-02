# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.utils import cint

from ez_accounting.api import apply_permitted_filter, ensure_admin, get_selected_company

no_cache = 1

# The only fields this page's inline edit is allowed to touch — kept as an
# explicit allowlist (rather than trusting whatever fieldname the client
# sends), same reasoning as items.py's ACCOUNT_FIELDS / binding.py's
# EDITABLE_FIELDS / customer.py's EDITABLE_FIELDS.
EDITABLE_FIELDS = {"supplier_name", "disabled", "supplier_type", "supplier_group"}

# Types that don't fit "Firstname Lastname" and are exempt from the format
# check below — unlike Customer (Individual/Company), Supplier also has
# "Partnership" ("Smith & Jones Partners"-style names, no more a personal
# name than a Company's is).
NON_INDIVIDUAL_SUPPLIER_TYPES = {"Company", "Partnership"}


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "supplier"
	context.title = _("Suppliers")
	context.company = get_selected_company()
	return context


INDIVIDUAL_NAME_PATTERN = re.compile(r"^[A-Za-z]+ [A-Za-z]+$")


def _validate_individual_name(raw: str) -> str:
	"""Hard-reject anything but exactly "Firstname Lastname" for Individual
	suppliers: two words, letters only — no commas, periods, hyphens,
	apostrophes, digits, or extra words. Checked here (and mirrored
	client-side) so a bad format is caught before it's ever saved, not
	silently reworded. Company/Partnership suppliers are never routed through
	this — see create_supplier()/update_supplier_field() below.
	"""
	raw = re.sub(r"\s+", " ", (raw or "")).strip()
	if not INDIVIDUAL_NAME_PATTERN.match(raw):
		frappe.throw(
			_('"{0}" is not a valid name — use exactly "Firstname Lastname": two words, letters only, no symbols.').format(
				raw
			)
		)
	return raw


@frappe.whitelist()
def get_suppliers():
	"""Every Supplier, shared across every company — see customer.py's
	get_customers() for the equivalent Customer-side note: the "Allowed To
	Transact With" restriction has been removed site-wide, so this is just a
	plain list, including disabled ones so this page can also be used to
	re-enable them.
	"""
	ensure_admin()

	return frappe.get_all(
		"Supplier",
		filters=apply_permitted_filter({}, "Supplier"),
		fields=["name", "supplier_name", "supplier_type", "supplier_group", "disabled"],
		order_by="supplier_name asc",
	)


@frappe.whitelist()
def get_supplier_groups():
	ensure_admin()
	return frappe.get_all("Supplier Group", fields=["name"], order_by="name asc")


@frappe.whitelist()
def update_supplier_field(name: str, field: str, value: str = ""):
	"""Inline edit of one field on an existing Supplier. Renaming an
	Individual always goes through the same "Firstname Lastname" enforcement
	as creating one — the whole point of this page is that the format doesn't
	regress the moment someone edits an existing record.
	"""
	ensure_admin()

	if field not in EDITABLE_FIELDS:
		frappe.throw(_("Invalid field: {0}").format(field))

	doc = frappe.get_doc("Supplier", name)

	if field == "supplier_name":
		new_name = (
			_validate_individual_name(value)
			if doc.supplier_type not in NON_INDIVIDUAL_SUPPLIER_TYPES
			else (value or "").strip()
		)
		if not new_name:
			frappe.throw(_("Name is required."))
		doc.supplier_name = new_name
	elif field == "disabled":
		doc.disabled = 1 if cint(value) else 0
	elif field == "supplier_type":
		new_type = (value or "").strip()
		if new_type not in ("Individual", "Company", "Partnership"):
			frappe.throw(_("Supplier Type must be Individual, Company, or Partnership."))
		# Switching an existing Company/Partnership-named supplier to
		# Individual would otherwise silently leave a name that violates the
		# very format this page exists to enforce — reject it the same way a
		# bad supplier_name edit is rejected, rather than letting the type
		# change alone quietly produce an invalid Individual record. The name
		# itself isn't touched here; the admin fixes it (in the Name field)
		# either before or after changing the type.
		if new_type not in NON_INDIVIDUAL_SUPPLIER_TYPES:
			_validate_individual_name(doc.supplier_name)
		doc.supplier_type = new_type
	elif field == "supplier_group":
		new_group = (value or "").strip()
		if not new_group or not frappe.db.exists("Supplier Group", new_group):
			frappe.throw(_('"{0}" is not a valid Supplier Group.').format(new_group))
		doc.supplier_group = new_group

	doc.save()

	return {"name": doc.name, "field": field, "value": doc.get(field)}


@frappe.whitelist()
def create_supplier(supplier_name: str, supplier_type: str = "Individual", supplier_group: str = ""):
	"""Add a new Supplier from this page. Individual names must be exactly
	"Firstname Lastname" — see _validate_individual_name(); Company/
	Partnership names are left exactly as typed, since those legitimately use
	commas/ampersands and don't fit that shape. Every new Supplier is usable
	from every company — there's no per-company restriction anymore.
	Supplier Group is optional — the doctype itself has no required/default
	value for it either, so leaving it blank here is left as-is, not coerced
	to some made-up default.
	"""
	ensure_admin()

	if supplier_type not in ("Individual", "Company", "Partnership"):
		frappe.throw(_("Supplier Type must be Individual, Company, or Partnership."))

	name = (
		_validate_individual_name(supplier_name)
		if supplier_type not in NON_INDIVIDUAL_SUPPLIER_TYPES
		else (supplier_name or "").strip()
	)
	if not name:
		frappe.throw(_("Name is required."))

	supplier_group = (supplier_group or "").strip()
	if supplier_group and not frappe.db.exists("Supplier Group", supplier_group):
		frappe.throw(_('"{0}" is not a valid Supplier Group.').format(supplier_group))

	doc = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": name,
			"supplier_type": supplier_type,
			"supplier_group": supplier_group or None,
		}
	)
	doc.insert()

	return {
		"name": doc.name,
		"supplier_name": doc.supplier_name,
		"supplier_type": doc.supplier_type,
		"supplier_group": doc.supplier_group,
		"disabled": doc.disabled,
	}

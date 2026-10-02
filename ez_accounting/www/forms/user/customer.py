# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.utils import cint

from ez_accounting.api import (
	apply_permitted_filter,
	ensure_admin,
	get_companies,
	get_selected_company,
	is_system_manager,
)

no_cache = 1

# The only fields this page's inline edit is allowed to touch — kept as an
# explicit allowlist (rather than trusting whatever fieldname the client
# sends), same reasoning as items.py's ACCOUNT_FIELDS / binding.py's
# EDITABLE_FIELDS.
EDITABLE_FIELDS = {
	"customer_name",
	"custom_middle_name",
	"disabled",
	"customer_type",
	"customer_group",
	"mobile_no",
	"tax_id",
	"territory",
	"custom_email",
	"custom_company",
}


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "customer"
	context.title = _("Customers")
	context.company = get_selected_company()
	context.companies = get_companies()
	# The "Show all customers" escape hatch is System-Manager-only — everyone
	# else is always scoped to the company they pick.
	context.can_show_all = is_system_manager()
	return context


def company_customer_names(company: str) -> set:
	"""Customer names that have any transaction with `company` — a Sales
	Invoice, a Payment Entry, or a Quotation. Customers aren't company-linked
	(see forms-company-sharing-removal), so this is what "customers for this
	company" derives from. Shared with ez_accounting.www.forms.user.sales so the
	Express Sales customer picker scopes the same way this page's list does."""
	names = set(frappe.get_all("Sales Invoice", filters={"company": company}, pluck="customer", distinct=True))
	names |= set(
		frappe.get_all(
			"Payment Entry",
			filters={"company": company, "party_type": "Customer"},
			pluck="party",
			distinct=True,
		)
	)
	names |= set(
		frappe.get_all(
			"Quotation",
			filters={"company": company, "quotation_to": "Customer"},
			pluck="party_name",
			distinct=True,
		)
	)
	names.discard(None)
	return names


def _own_customer_names() -> set:
	"""Customer names created by the current session user. A customer added
	from this page has no transaction yet, so company_customer_names() won't
	include it — but the person who just created it needs to still see it
	(they have no "Show all" unless they're a System Manager). `owner` is set
	by Frappe on insert, so this is exactly "customers I added"."""
	return set(
		frappe.get_all("Customer", filters={"owner": frappe.session.user}, pluck="name")
	)


def _validate_individual_name(raw: str) -> str:
	"""Enforce "Firstname Lastname" order (not "Lastname, Firstname") for
	Individual customers — the one thing this page exists to stop
	perpetuating, since most of this system's historical data was entered
	comma-first (see forms-cbc-customer-linking). The only hard rule is
	**no comma**: middle names, suffixes ("Maria Cruz III"), hyphens
	("Mary-Jane"), apostrophes ("O'Brien") and accented letters ("José
	Núñez") are all legitimate names and pass through as typed. Mirrored
	client-side so it's caught before a round trip. Company-type customers
	(orgs, trusts, "X, LLC" names) are never routed through this — see
	create_customer()/update_customer_field() below.
	"""
	raw = re.sub(r"\s+", " ", (raw or "")).strip()
	if not raw:
		frappe.throw(_("Name is required."))
	if "," in raw:
		frappe.throw(
			_('"{0}" is not a valid name — remove the comma and enter it as "Firstname Lastname", not "Lastname, Firstname".').format(
				raw
			)
		)
	return raw


def _normalize_customer_name(raw: str, customer_type: str) -> str:
	"""Normalize a Customer name according to its party type.

	Company customer names are stored in uppercase so the rule is consistent
	outside this portal too (reports, link fields, and exports). Individual
	names retain their original capitalization and existing validation rules.
	"""
	if customer_type == "Individual":
		return _validate_individual_name(raw)
	return (raw or "").strip().upper()


@frappe.whitelist()
def get_customers(company: str = "", show_all=0):
	"""Customers for the list, including disabled ones (so this page can also
	re-enable them). Narrowed to the Customers the user is permitted, when
	they have ERPNext User Permission records for Customer.

	`company` (default: the selected company) filters to customers with a
	transaction for it — see company_customer_names() — plus any customer
	this user created themselves (so a just-added customer, which has no
	transaction yet, doesn't vanish from the list the moment it's saved).
	`show_all` truthy drops the company filter and lists every customer (the
	state before this page was company-filtered). Customers themselves aren't
	company-linked, so this is a view filter, not a permission scope.
	"""
	ensure_admin()

	filters = apply_permitted_filter([], "Customer")
	company = (company or "").strip() or get_selected_company()
	# `show_all` is honoured only for System Managers — clamped off for
	# everyone else so a scoped user is always company-filtered.
	if company and not (int(show_all or 0) and is_system_manager()):
		assigned = set(frappe.get_all("Customer", filters={"custom_company": company}, pluck="name"))
		legacy_owned = set(
			frappe.get_all("Customer", filters={"owner": frappe.session.user, "custom_company": ["is", "not set"]}, pluck="name")
		)
		allowed = company_customer_names(company) | assigned | legacy_owned
		if not allowed:
			return []
		filters.append(["name", "in", list(allowed)])

	return frappe.get_all(
		"Customer",
		filters=filters,
		fields=["name", "customer_name", "custom_middle_name", "customer_type", "customer_group", "custom_company", "disabled"],
		order_by="customer_name asc",
	)


@frappe.whitelist()
def get_customer_groups():
	ensure_admin()
	return frappe.get_all("Customer Group", fields=["name"], order_by="name asc")


@frappe.whitelist()
def update_customer_field(name: str, field: str, value: str = ""):
	"""Inline edit of one field on an existing Customer. Renaming an
	Individual always goes through the same "Firstname Lastname" enforcement
	as creating one — the whole point of this page is that the format doesn't
	regress the moment someone edits an existing record.

	When customer_name changes, the document's own ID (docname) is renamed to
	match it too — new customers already get name-as-ID for free (Selling
	Settings' "Customer Naming By" = Customer Name), but a plain field save
	never touches an *existing* doc's docname, so without this a renamed
	customer's ID would silently drift out of sync with its display name.
	"""
	ensure_admin()

	if field not in EDITABLE_FIELDS:
		frappe.throw(_("Invalid field: {0}").format(field))

	doc = frappe.get_doc("Customer", name)

	if field == "customer_name":
		new_name = _normalize_customer_name(value, doc.customer_type)
		if not new_name:
			frappe.throw(_("Name is required."))

		# Checked before touching anything, so a rejected rename leaves the
		# customer completely unchanged instead of saving the new display
		# name but failing to move the ID to match it.
		if new_name != doc.name and frappe.db.exists("Customer", new_name):
			frappe.throw(
				_('A customer named "{0}" already exists — pick a different name, or that customer needs renaming/merging first.').format(
					new_name
				)
			)

		doc.customer_name = new_name
		doc.save()

		if new_name != doc.name:
			frappe.rename_doc("Customer", doc.name, new_name)
			doc = frappe.get_doc("Customer", new_name)
	elif field == "custom_middle_name":
		doc.custom_middle_name = (value or "").strip()
		doc.save()
	elif field == "disabled":
		doc.disabled = 1 if cint(value) else 0
		doc.save()
	elif field == "customer_type":
		new_type = (value or "").strip()
		if new_type not in ("Individual", "Company"):
			frappe.throw(_("Customer Type must be Individual or Company."))
		# Switching an existing Company-named customer ("CWM Trust, LLC") to
		# Individual would otherwise silently leave a name that violates the
		# very format this page exists to enforce — reject it the same way a
		# bad customer_name edit is rejected, rather than letting the type
		# change alone quietly produce an invalid Individual record. The name
		# itself isn't touched here; the admin fixes it (in the Name field)
		# either before or after changing the type.
		if new_type == "Individual":
			_validate_individual_name(doc.customer_name)
		doc.customer_type = new_type
		if new_type == "Company":
			doc.customer_name = _normalize_customer_name(doc.customer_name, new_type)
		doc.save()
	elif field == "customer_group":
		new_group = (value or "").strip()
		if not new_group or not frappe.db.exists("Customer Group", new_group):
			frappe.throw(_('"{0}" is not a valid Customer Group.').format(new_group))
		doc.customer_group = new_group
		doc.save()
	elif field == "mobile_no":
		doc.mobile_no = (value or "").strip()
		doc.save()
	elif field == "tax_id":
		doc.tax_id = (value or "").strip()
		doc.save()
	elif field == "territory":
		new_territory = (value or "").strip()
		if new_territory and not frappe.db.exists("Territory", new_territory):
			frappe.throw(_('"{0}" is not a valid Territory.').format(new_territory))
		doc.territory = new_territory or None
		doc.save()
	elif field == "custom_email":
		# Same field the Student profile page uses (see
		# [[forms-student-customer]]'s 2026-09-04 entry) -- here it doubles as
		# a writable email for any Customer, since the native email_id is a
		# read-only fetch from customer_primary_contact (not editable here).
		doc.custom_email = (value or "").strip() or None
		doc.save()
	elif field == "custom_company":
		company = (value or "").strip()
		if not company or not frappe.db.exists("Company", company):
			frappe.throw(_('{0} is not a valid Company.').format(company or _("Blank")))
		doc.custom_company = company
		doc.save()

	return {"name": doc.name, "field": field, "value": doc.get(field)}


@frappe.whitelist()
def update_customer_details(
	name: str,
	customer_name: str,
	customer_type: str,
	customer_group: str,
	company: str,
	custom_middle_name: str = "",
	disabled=0,
):
	"""Save every field shown by the portal's explicit Edit dialog at once."""
	ensure_admin()

	if customer_type not in ("Individual", "Company"):
		frappe.throw(_("Customer Type must be Individual or Company."))

	new_name = _normalize_customer_name(customer_name, customer_type)
	if not new_name:
		frappe.throw(_("Name is required."))

	customer_group = (customer_group or "").strip()
	if not customer_group or not frappe.db.exists("Customer Group", customer_group):
		frappe.throw(_('"{0}" is not a valid Customer Group.').format(customer_group))

	company = (company or "").strip()
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_('{0} is not a valid Company.').format(company or _("Blank")))

	doc = frappe.get_doc("Customer", name)
	if new_name != doc.name and frappe.db.exists("Customer", new_name):
		frappe.throw(_('A customer named "{0}" already exists.').format(new_name))

	doc.customer_name = new_name
	doc.customer_type = customer_type
	doc.customer_group = customer_group
	doc.custom_company = company
	doc.custom_middle_name = (custom_middle_name or "").strip() if customer_type == "Individual" else ""
	doc.disabled = 1 if cint(disabled) else 0
	doc.save()

	if new_name != doc.name:
		frappe.rename_doc("Customer", doc.name, new_name)

	return {"name": new_name}


@frappe.whitelist()
def create_customer(
	customer_name: str,
	customer_type: str = "Individual",
	customer_group: str = "",
	custom_middle_name: str = "",
	company: str = "",
):
	"""Add a new Customer from this page. Individual names must be exactly
	"Firstname Lastname" — see _validate_individual_name(); Company names are
	stored in uppercase, while punctuation such as commas is preserved. Every
	new Customer is usable
	from every company — there's no per-company restriction anymore.
	Customer Group is optional — the doctype itself has no required/default
	value for it either, so leaving it blank here is left as-is, not coerced
	to some made-up default.
	"""
	ensure_admin()

	if customer_type not in ("Individual", "Company"):
		frappe.throw(_("Customer Type must be Individual or Company."))

	name = _normalize_customer_name(customer_name, customer_type)
	if not name:
		frappe.throw(_("Name is required."))

	customer_group = (customer_group or "").strip()
	if customer_group and not frappe.db.exists("Customer Group", customer_group):
		frappe.throw(_('"{0}" is not a valid Customer Group.').format(customer_group))
	company = (company or "").strip() or get_selected_company()
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("A valid Company is required."))

	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name,
			"customer_type": customer_type,
			"customer_group": customer_group or None,
			"custom_company": company,
			# Only meaningful for Individuals -- a Company-named customer has no
			# "middle name" concept, so a stray value here is silently dropped
			# rather than saved onto a record it doesn't apply to.
			"custom_middle_name": (custom_middle_name or "").strip() if customer_type == "Individual" else "",
		}
	)
	doc.insert()

	return {
		"name": doc.name,
		"customer_name": doc.customer_name,
		"custom_middle_name": doc.custom_middle_name,
		"customer_type": doc.customer_type,
		"customer_group": doc.customer_group,
		"custom_company": doc.custom_company,
		"disabled": doc.disabled,
	}


@frappe.whitelist()
def delete_customer(name: str):
	"""Delete a Customer outright — only actually succeeds if nothing
	references it (Frappe's own link-check, same protection Desk's delete
	uses, and the same one items.py's delete_item() relies on); a linked
	customer (used on any Sales Invoice, Payment Entry, Quotation, etc.)
	raises LinkExistsError with the specific linked records listed, surfaced
	to the admin as-is rather than caught and reworded — the confirm() dialog
	on the client already sets the expectation that a delete can be blocked
	this way. Disabling (via update_customer_field) is the safe default for
	a customer that's just no longer active but has real history — delete is
	only for a genuinely unused/mistaken entry.
	"""
	ensure_admin()

	frappe.delete_doc("Customer", name)
	frappe.db.commit()

	return {"deleted": name}

# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

"""A single (non-student) Customer's profile — basic fields plus a sales
summary, reached from the "Profile" link next to a customer's name on
/forms/user/sales' own Sales Invoice History (previously student-only, via
/forms/user/student_customer_profile; every Customer now gets a profile link,
students still go to their own richer page — see that link's own comment in
sales.html for why the two are kept separate rather than merged).

Deliberately much leaner than student_customer_profile.html — no vacations,
courses, documents, credit notes, or the printed Statement of Account
machinery, none of which make sense for a Customer that isn't a student.
Editing reuses ez_accounting.www.forms.user.customer's own update_customer_field()
directly (one allowlist, one place field edits land, regardless of which
page is looking at a given Customer) rather than a second copy of the same
logic.
"""

import frappe
from frappe import _
from frappe.utils import flt

from ez_accounting.api import apply_permitted_filter, ensure_admin, permitted_docs

no_cache = 1


def get_context(context):
	ensure_admin()
	context.body_class = "forms-portal-dark"
	context.nav_active = "customer_profile"
	context.title = _("Customer Profile")
	context.customer = frappe.form_dict.get("customer") or ""
	return context


def _ensure_customer(name: str):
	if not frappe.db.exists("Customer", name):
		frappe.throw(_("{0} is not a Customer.").format(name))
	permitted = permitted_docs("Customer")
	if permitted is not None and name not in permitted:
		frappe.throw(_("You are not permitted to view {0}.").format(name), frappe.PermissionError)


@frappe.whitelist()
def get_customer(name: str):
	"""Every field this profile shows/edits, in one call — same "no reason to
	make a single-customer page wait on more than one round trip" reasoning
	student_customer.py's own get_student() docstring already gives.
	"""
	ensure_admin()
	_ensure_customer(name)

	doc = frappe.get_doc("Customer", name)
	return {
		"name": doc.name,
		"customer_name": doc.customer_name,
		"customer_type": doc.customer_type,
		"customer_group": doc.customer_group,
		"territory": doc.territory,
		"mobile_no": doc.mobile_no,
		"email_id": doc.email_id,
		"custom_email": doc.get("custom_email"),
		"tax_id": doc.tax_id,
		"disabled": doc.disabled,
	}


@frappe.whitelist()
def get_customer_groups():
	return frappe.get_all("Customer Group", fields=["name"], order_by="name asc")


@frappe.whitelist()
def get_territories():
	return frappe.get_all("Territory", fields=["name"], order_by="name asc")


@frappe.whitelist()
def get_customer_sales_summary(name: str):
	"""This customer's full Sales Invoice history plus payment detail and a
	Total Billed/Paid/Balance Due summary. The Customer-generic counterpart
	of student_customer.get_student_sales_summary() — same computation
	(applied-to-invoice payments + on-account advance, see
	[[forms-express-sales]]), just without that function's Student Customer
	Group gate, since this page is reached from every customer's row on
	/forms/user/sales, not only students.
	"""
	ensure_admin()
	_ensure_customer(name)

	invoices = frappe.get_all(
		"Sales Invoice",
		filters=apply_permitted_filter({"customer": name, "docstatus": 1}, "Company", "company"),
		fields=["name", "posting_date", "company", "currency", "grand_total", "outstanding_amount", "status"],
		order_by="posting_date desc, creation desc",
	)

	invoice_names = [inv.name for inv in invoices]
	payments_by_invoice = {}
	total_paid = 0.0
	if invoice_names:
		refs = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_doctype": "Sales Invoice", "reference_name": ["in", invoice_names], "docstatus": 1},
			fields=["parent", "reference_name", "allocated_amount"],
		)
		if refs:
			pe_names = sorted({r.parent for r in refs})
			pe_by_name = {
				row.name: row
				for row in frappe.get_all(
					"Payment Entry",
					filters={"name": ["in", pe_names]},
					fields=["name", "posting_date", "mode_of_payment", "reference_no",
					        "unallocated_amount", "paid_amount"],
				)
			}
			for ref in refs:
				pe = pe_by_name.get(ref.parent)
				if not pe:
					continue
				payments_by_invoice.setdefault(ref.reference_name, []).append(
					{
						"posting_date": pe.posting_date,
						# "applied", not "amount" -- matches the shape
						# render_sales_summary() (copied from
						# student_customer_profile.html) already expects.
						"applied": ref.allocated_amount,
						"unallocated_amount": pe.unallocated_amount,
						"paid_amount": pe.paid_amount,
						"mode_of_payment": pe.mode_of_payment,
						"reference_no": pe.reference_no,
					}
				)
				total_paid += ref.allocated_amount
			for rows in payments_by_invoice.values():
				rows.sort(key=lambda r: (r["posting_date"] or frappe.utils.getdate("1900-01-01")), reverse=True)

	for inv in invoices:
		inv["payments"] = payments_by_invoice.get(inv.name, [])

	advance_amount = 0.0
	for pe in frappe.get_all(
		"Payment Entry",
		filters=apply_permitted_filter(
			{
				"party_type": "Customer",
				"party": name,
				"payment_type": "Receive",
				"docstatus": 1,
				"unallocated_amount": [">", 0],
			},
			"Company",
			"company",
		),
		fields=["unallocated_amount"],
	):
		advance_amount += flt(pe.unallocated_amount)

	total_outstanding = sum(inv.outstanding_amount or 0 for inv in invoices)

	return {
		"invoices": invoices,
		"total_outstanding": total_outstanding,
		"total_billed": sum(inv.grand_total or 0 for inv in invoices),
		"total_paid": total_paid + advance_amount,
		"advance_amount": advance_amount,
		"balance_due": total_outstanding - advance_amount,
		"invoice_count": len(invoices),
	}

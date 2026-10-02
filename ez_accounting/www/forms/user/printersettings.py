# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _

from ez_accounting.api import ensure_admin, get_print_format_settings, get_selected_company

no_cache = 1


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "printersettings"
	context.title = _("Print Settings")
	context.company = get_selected_company()
	return context


@frappe.whitelist()
def get_settings():
	"""The selected company's current Forms Print Settings, for this page's
	own form to populate from — thin wrapper around
	ez_accounting.api.get_print_format_settings() (also used directly by Express
	Sales/Purchase to resolve their Invoice/Receipt/Check links) so this
	page's JS doesn't need to know that function's exact module path.
	"""
	ensure_admin()
	return get_print_format_settings()


@frappe.whitelist()
def get_print_formats(doctype: str):
	"""Enabled Print Formats for one doctype — same query as
	ez_accounting.www.forms.user.printer.get_print_formats(), duplicated here rather
	than imported so this page stays fully self-contained like every other
	forms portal page.
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


@frappe.whitelist()
def save_settings(
	sales_invoice_format: str = "",
	sales_receipt_format: str = "",
	purchase_invoice_format: str = "",
	purchase_receipt_format: str = "",
	purchase_check_format: str = "",
	payroll_check_format: str = "",
):
	"""Create or update the selected company's one Forms Print Settings row
	(autoname: field:company, same pattern as Forms Payroll Tax Settings).
	Every field is optional — a blank value clears that slot back to its
	hardcoded fallback in sales.py/purchase.py/payroll.py, it isn't an error.
	"""
	ensure_admin()

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	def _validate(value, doctype, label):
		# Each format, if given, must actually belong to the doctype it's
		# meant for — catches e.g. picking a Sales Invoice format for the
		# Purchase Invoice slot before it's ever saved, rather than silently
		# storing something that can only fail later when a real link tries
		# to use it.
		value = (value or "").strip()
		if not value:
			return None
		actual_doctype = frappe.db.get_value("Print Format", value, "doc_type")
		if not actual_doctype:
			frappe.throw(_("{0} is not a valid Print Format.").format(value))
		if actual_doctype != doctype:
			frappe.throw(_("{0} must be a Print Format for {1}.").format(label, doctype))
		return value

	values = {
		"sales_invoice_format": _validate(sales_invoice_format, "Sales Invoice", _("Sales Invoice Format")),
		"sales_receipt_format": _validate(sales_receipt_format, "Payment Entry", _("Sales Receipt Format")),
		"purchase_invoice_format": _validate(
			purchase_invoice_format, "Purchase Invoice", _("Purchase Invoice Format")
		),
		"purchase_receipt_format": _validate(
			purchase_receipt_format, "Payment Entry", _("Purchase Receipt Format")
		),
		"purchase_check_format": _validate(
			purchase_check_format, "Payment Entry", _("Purchase Check Format")
		),
		"payroll_check_format": _validate(
			payroll_check_format, "Salary Slip", _("Payroll Check Format")
		),
	}

	if frappe.db.exists("Forms Print Settings", company):
		doc = frappe.get_doc("Forms Print Settings", company)
	else:
		doc = frappe.new_doc("Forms Print Settings")
		doc.company = company

	doc.update(values)
	doc.save()
	frappe.db.commit()

	return values

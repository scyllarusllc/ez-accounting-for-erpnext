# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate

from ez_accounting.api import apply_permitted_filter, ensure_admin, get_selected_company

no_cache = 1

# Every existing Employee on this site uses this one naming series (confirmed
# live — it's also the doctype's only real option) — hardcoded here rather
# than exposed as a form field, same reasoning as create_item()'s hardcoded
# stock_uom fallback.
NAMING_SERIES = "HR-EMP-"

# Requested default for new employees added through this page — HRMS itself
# has no default at the doctype level (date_of_joining is mandatory with no
# fallback), so this page supplies one.
DEFAULT_DATE_OF_JOINING = "2000-01-01"

# The only fields this page's inline edit is allowed to touch — kept as an
# explicit allowlist (rather than trusting whatever fieldname the client
# sends), same reasoning as customer.py's/supplier.py's/items.py's own
# EDITABLE_FIELDS. Deliberately doesn't include "company" — reassigning an
# employee's company has broad payroll/GL implications this page has no
# business touching.
EDITABLE_FIELDS = {
	"first_name",
	"middle_name",
	"last_name",
	"custom_ssn",
	"date_of_birth",
	"date_of_joining",
	"gender",
	"holiday_list",
	"status",
}


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "employee"
	context.title = _("Employees")
	context.company = get_selected_company()
	return context


def _resolve_default_holiday_list(company: str) -> str | None:
	"""Best-guess default Holiday List for `company`, for a new employee left
	blank or an existing one missing one entirely. Prefers the Company's own
	`default_holiday_list` (the field HRMS itself already reads the same way
	for attendance/LWP calculations — see hrms.hr.utils.get_holiday_dates())
	— falling back to the sole Holiday List on the site if there's exactly
	one (this bench currently has exactly one, "TEST Holiday List 2026"),
	same "sole X" fallback style as payroll.py's _ensure_default_bank_account()
	/items.py's _resolve_default_warehouse(). Returns None (left blank, which
	validates fine) if neither resolves.
	"""
	default_list = frappe.db.get_value("Company", company, "default_holiday_list")
	if default_list:
		return default_list

	lists = frappe.get_all("Holiday List", fields=["name"], order_by="name asc")
	return lists[0].name if len(lists) == 1 else None


@frappe.whitelist()
def get_genders():
	ensure_admin()
	return frappe.get_all("Gender", fields=["name"], order_by="name asc")


@frappe.whitelist()
def get_holiday_lists():
	ensure_admin()
	return frappe.get_all("Holiday List", fields=["name"], order_by="name asc")


@frappe.whitelist()
def get_employees():
	"""Every Employee for the selected company, including inactive/left ones
	so this page can also be used to re-enable them. Backfills `holiday_list`
	for anyone missing one entirely (a real, pre-existing gap confirmed live
	— every Active employee on this site had it blank before this page
	existed) via a direct field write rather than a full doc save, since this
	runs across every employee on every page load and the only thing being
	set is a single already-validated Link value.
	"""
	ensure_admin()

	company = get_selected_company()
	if not company:
		return []

	employees = frappe.get_all(
		"Employee",
		filters=apply_permitted_filter({"company": company}, "Employee"),
		fields=[
			"name",
			"first_name",
			"middle_name",
			"last_name",
			"employee_name",
			"custom_ssn",
			"date_of_birth",
			"date_of_joining",
			"gender",
			"holiday_list",
			"status",
		],
		order_by="employee_name asc",
	)

	default_holiday_list = None
	for emp in employees:
		if emp.holiday_list:
			continue
		if default_holiday_list is None:
			default_holiday_list = _resolve_default_holiday_list(company) or ""
		if default_holiday_list:
			try:
				frappe.db.set_value("Employee", emp.name, "holiday_list", default_holiday_list, update_modified=False)
				emp["holiday_list"] = default_holiday_list
			except Exception:
				frappe.log_error(frappe.get_traceback(), "Employee default Holiday List backfill failed")

	return employees


@frappe.whitelist()
def create_employee(
	first_name: str,
	middle_name: str = "",
	last_name: str = "",
	gender: str = "",
	date_of_birth: str = "",
	date_of_joining: str = "",
	custom_ssn: str = "",
	holiday_list: str = "",
):
	"""Add a new Employee from this page. employee_name (the display "Full
	Name") isn't set here — Employee.set_employee_name(), called from its own
	validate(), derives it automatically from first/middle/last name on every
	save, so it can never drift out of sync with them.
	"""
	ensure_admin()

	first_name = (first_name or "").strip()
	if not first_name:
		frappe.throw(_("First Name is required."))

	if not gender or not frappe.db.exists("Gender", gender):
		frappe.throw(_("Select a valid Gender."))

	if not date_of_birth:
		frappe.throw(_("Date of Birth is required."))

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	date_of_joining = date_of_joining or DEFAULT_DATE_OF_JOINING
	holiday_list = (holiday_list or "").strip() or _resolve_default_holiday_list(company)

	doc = frappe.get_doc(
		{
			"doctype": "Employee",
			"naming_series": NAMING_SERIES,
			"first_name": first_name,
			"middle_name": (middle_name or "").strip() or None,
			"last_name": (last_name or "").strip() or None,
			"gender": gender,
			"date_of_birth": getdate(date_of_birth),
			"date_of_joining": getdate(date_of_joining),
			"company": company,
			"status": "Active",
			"custom_ssn": (custom_ssn or "").strip() or None,
			"holiday_list": holiday_list or None,
		}
	)
	doc.insert()

	return {
		"name": doc.name,
		"employee_name": doc.employee_name,
		"first_name": doc.first_name,
		"middle_name": doc.middle_name,
		"last_name": doc.last_name,
		"custom_ssn": doc.custom_ssn,
		"date_of_birth": doc.date_of_birth,
		"date_of_joining": doc.date_of_joining,
		"gender": doc.gender,
		"holiday_list": doc.holiday_list,
		"status": doc.status,
	}


@frappe.whitelist()
def update_employee_field(name: str, field: str, value: str = ""):
	"""Inline edit of one field on an existing Employee. Clearing Holiday
	List back to blank re-resolves the default rather than leaving it
	genuinely empty — this page treats "no Holiday List" as a state that's
	always automatically fixed, not a valid end state, per the same
	reasoning as _resolve_default_holiday_list()/get_employees()'s own
	backfill above.
	"""
	ensure_admin()

	if field not in EDITABLE_FIELDS:
		frappe.throw(_("Invalid field: {0}").format(field))

	doc = frappe.get_doc("Employee", name)

	if field == "first_name":
		value = (value or "").strip()
		if not value:
			frappe.throw(_("First Name is required."))
		doc.first_name = value
	elif field in ("middle_name", "last_name"):
		doc.set(field, (value or "").strip() or None)
	elif field == "custom_ssn":
		doc.custom_ssn = (value or "").strip() or None
	elif field == "date_of_birth":
		if not value:
			frappe.throw(_("Date of Birth is required."))
		doc.date_of_birth = getdate(value)
	elif field == "date_of_joining":
		if not value:
			frappe.throw(_("Date of Joining is required."))
		doc.date_of_joining = getdate(value)
	elif field == "gender":
		if not value or not frappe.db.exists("Gender", value):
			frappe.throw(_("Select a valid Gender."))
		doc.gender = value
	elif field == "status":
		if value not in ("Active", "Inactive", "Suspended", "Left"):
			frappe.throw(_("Invalid status: {0}").format(value))
		doc.status = value
	elif field == "holiday_list":
		value = (value or "").strip()
		if not value:
			value = _resolve_default_holiday_list(doc.company)
		elif not frappe.db.exists("Holiday List", value):
			frappe.throw(_("{0} is not a valid Holiday List.").format(value))
		doc.holiday_list = value or None

	doc.save()

	return {"name": doc.name, "field": field, "value": doc.get(field), "employee_name": doc.employee_name}

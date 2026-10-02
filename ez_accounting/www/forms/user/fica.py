# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt

from ez_accounting.api import (
	ensure_logged_in,
	get_date_range_with_default,
	get_quick_ranges,
	get_salary_slip_component_breakdown,
	get_selected_company,
)

no_cache = 1

# Settings doctype field -> employee-breakdown column. The Salary Component
# each field points to is configured per company via Forms Payroll Tax
# Settings (Desk) — there's no single hardcoded component name that's correct
# across companies, since each company sets up its own Salary Components.
FICA_SETTINGS_FIELDS = {
	"fica_ss_employee_component": "ss_ee",
	"fica_medicare_employee_component": "med_ee",
	"fica_ss_employer_component": "ss_er",
	"fica_medicare_employer_component": "med_er",
}


def _get_fica_components(company: str) -> dict[str, str]:
	"""Salary Component name -> breakdown column, for this company. Empty dict
	if the company has no Forms Payroll Tax Settings row, or has one but never
	filled in any of the FICA fields.
	"""
	settings = frappe.db.get_value(
		"Forms Payroll Tax Settings", company, list(FICA_SETTINGS_FIELDS), as_dict=True
	)
	if not settings:
		return {}
	return {
		settings[field]: column
		for field, column in FICA_SETTINGS_FIELDS.items()
		if settings.get(field)
	}


def _build_breakdown(
	company: str, from_date: str | None, to_date: str | None
) -> tuple[list[dict], dict, dict]:
	components = _get_fica_components(company)
	if not components:
		return [], {}, {}

	rows = get_salary_slip_component_breakdown(
		list(components), company, from_date=from_date, to_date=to_date, date_field="end_date"
	)
	slips = {}
	for row in rows:
		slip = slips.setdefault(
			row.salary_slip,
			{
				"employee": row.employee,
				"employee_name": row.employee_name,
				"start_date": row.start_date,
				"end_date": row.end_date,
				"days_worked": flt(row.payment_days),
				"gross_pay": flt(row.gross_pay),
				"ss_ee": 0,
				"med_ee": 0,
				"ss_er": 0,
				"med_er": 0,
			},
		)
		key = components.get(row.salary_component)
		if key:
			slip[key] = flt(row.amount)

	for slip in slips.values():
		slip["total"] = slip["ss_ee"] + slip["med_ee"] + slip["ss_er"] + slip["med_er"]

	employee_breakdown = sorted(
		slips.values(), key=lambda e: (e["employee_name"] or "", e["start_date"] or "")
	)
	breakdown_totals = {
		key: sum(row[key] for row in employee_breakdown)
		for key in ("gross_pay", "ss_ee", "med_ee", "ss_er", "med_er", "total")
	}
	return employee_breakdown, breakdown_totals, components


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "fica"
	context.title = _("FICA — SS/Medicare Payable")

	company = get_selected_company()
	context.company = company

	from_date, to_date = get_date_range_with_default()
	context.from_date = from_date
	context.to_date = to_date

	quick = get_quick_ranges(from_date, to_date)
	context.this_month_from, context.this_month_to = quick["this_month"]
	context.last_month_from, context.last_month_to = quick["last_month"]
	context.quick_active = quick["active"]

	employee_breakdown, breakdown_totals, components = _build_breakdown(company, from_date, to_date)
	context.employee_breakdown = employee_breakdown
	context.breakdown_totals = breakdown_totals
	context.not_configured = not components
	return context


@frappe.whitelist()
def download_excel():
	ensure_logged_in()

	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	from_date, to_date = get_date_range_with_default()
	employee_breakdown, breakdown_totals, components = _build_breakdown(company, from_date, to_date)

	if not components:
		frappe.throw(
			_("FICA components aren't configured for {0} yet — set them up in Forms Payroll Tax Settings.").format(
				company
			)
		)

	header = [
		_("Employee"),
		_("Start Date"),
		_("End Date"),
		_("Days Worked"),
		_("Gross Pay"),
		_("SS (Employee)"),
		_("Medicare (Employee)"),
		_("SS (Employer)"),
		_("Medicare (Employer)"),
		_("Total"),
	]
	data = [header]
	for row in employee_breakdown:
		data.append(
			[
				row["employee_name"] or row["employee"],
				row["start_date"],
				row["end_date"],
				row["days_worked"],
				row["gross_pay"],
				row["ss_ee"],
				row["med_ee"],
				row["ss_er"],
				row["med_er"],
				row["total"],
			]
		)
	data.append(
		[
			_("All Employees"),
			"",
			"",
			"",
			breakdown_totals["gross_pay"],
			breakdown_totals["ss_ee"],
			breakdown_totals["med_ee"],
			breakdown_totals["ss_er"],
			breakdown_totals["med_er"],
			breakdown_totals["total"],
		]
	)

	build_xlsx_response(data, f"FICA Breakdown {from_date} to {to_date}")

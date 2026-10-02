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


def _get_withholding_component(company: str) -> str | None:
	"""Salary Component name for this company's local/withholding tax, from
	Forms Payroll Tax Settings. None if the company has no settings row, or
	has one but never filled in this field.
	"""
	return frappe.db.get_value(
		"Forms Payroll Tax Settings", company, "treasurer_withholding_component"
	)


def _build_breakdown(
	company: str, from_date: str | None, to_date: str | None
) -> tuple[list[dict], dict, str | None]:
	component = _get_withholding_component(company)
	if not component:
		return [], {}, None

	rows = get_salary_slip_component_breakdown(
		[component], company, from_date=from_date, to_date=to_date, date_field="end_date"
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
				"amount": 0,
			},
		)
		if row.salary_component == component:
			slip["amount"] = flt(row.amount)

	employee_breakdown = sorted(
		slips.values(), key=lambda e: (e["employee_name"] or "", e["start_date"] or "")
	)
	breakdown_totals = {
		key: sum(row[key] for row in employee_breakdown) for key in ("gross_pay", "amount")
	}
	return employee_breakdown, breakdown_totals, component


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "treasurer"
	context.title = _("CNMI Treasurer — Withholding Tax Payable")

	company = get_selected_company()
	context.company = company

	from_date, to_date = get_date_range_with_default()
	context.from_date = from_date
	context.to_date = to_date

	quick = get_quick_ranges(from_date, to_date)
	context.this_month_from, context.this_month_to = quick["this_month"]
	context.last_month_from, context.last_month_to = quick["last_month"]
	context.quick_active = quick["active"]

	employee_breakdown, breakdown_totals, component = _build_breakdown(company, from_date, to_date)
	context.employee_breakdown = employee_breakdown
	context.breakdown_totals = breakdown_totals
	context.not_configured = not component
	return context


@frappe.whitelist()
def download_excel():
	ensure_logged_in()

	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	from_date, to_date = get_date_range_with_default()
	employee_breakdown, breakdown_totals, component = _build_breakdown(company, from_date, to_date)

	if not component:
		frappe.throw(
			_(
				"The Withholding Tax component isn't configured for {0} yet — set it up in Forms Payroll Tax Settings."
			).format(company)
		)

	header = [
		_("Employee"),
		_("Start Date"),
		_("End Date"),
		_("Days Worked"),
		_("Gross Pay"),
		_("Withholding Tax"),
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
				row["amount"],
			]
		)
	data.append(
		[_("All Employees"), "", "", "", breakdown_totals["gross_pay"], breakdown_totals["amount"]]
	)

	build_xlsx_response(data, f"Treasurer Withholding {from_date} to {to_date}")

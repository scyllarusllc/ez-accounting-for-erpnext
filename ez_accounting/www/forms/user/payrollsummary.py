# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt

from ez_accounting.api import (
	ensure_logged_in,
	get_date_range_with_default,
	get_quick_ranges,
	get_selected_company,
)

no_cache = 1


def _build_summary(
	company: str, from_date: str | None, to_date: str | None, reference: str | None = None,
	ref_from: str | None = None, ref_to: str | None = None,
) -> tuple[list[dict], dict]:
	"""One row per submitted Salary Slip whose pay period end date falls in
	range — same "one row per slip, not merged across slips" reasoning as
	fica.py's own Employee Breakdown (see get_salary_slip_component_breakdown()'s
	docstring): grouping by employee alone can't attach a single Start/End
	Date or Days Worked to a row when an employee has more than one slip in
	range. Unlike FICA/Treasurer, this doesn't need the Salary Detail
	component-join machinery at all — Gross Pay/Total Deduction/Net Pay are
	already plain fields directly on Salary Slip itself.
	"""
	filters = [["docstatus", "=", 1], ["company", "=", company]]
	if from_date:
		filters.append(["end_date", ">=", from_date])
	if to_date:
		filters.append(["end_date", "<=", to_date])

	slips = frappe.get_all(
		"Salary Slip",
		filters=filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"posting_date",
			"start_date",
			"end_date",
			"payment_days",
			"payroll_frequency",
			"gross_pay",
			"total_deduction",
			"net_pay",
			"payroll_entry",
		],
		order_by="posting_date asc, end_date asc, employee_name asc",
	)

	bank_entry_details = _get_bank_entry_details({s.payroll_entry for s in slips if s.payroll_entry})
	for row in slips:
		details = bank_entry_details.get(row.payroll_entry) or {}
		row["reference_no"] = details.get("cheque_no")
		row["custom_remarks"] = details.get("remark") if details.get("custom_remark") else None

	# Date oldest to newest is the main order; within the same Date the
	# Reference follows its series, smallest to largest (numeric when the
	# reference is a number, e.g. check numbers). Slips with no Reference go
	# last within their Date.
	def _ref_key(ref):
		ref = ref or ""
		return (0, int(ref), "") if ref.isdigit() else (1, 0, ref)

	slips.sort(
		key=lambda s: (
			s["posting_date"],
			_ref_key(s["reference_no"]) if s["reference_no"] else (2, 0, ""),
			s["end_date"],
			s["employee_name"] or "",
		)
	)

	# Options for the Reference filter come from the whole date range, before
	# the filter itself narrows the rows.
	# Ordered by series, smallest to largest.
	references = sorted({s["reference_no"] for s in slips if s["reference_no"]}, key=_ref_key)
	if reference:
		slips = [s for s in slips if s["reference_no"] == reference]
	# Reference From/To: inclusive range along the same series order.
	if ref_from or ref_to:
		lo = references.index(ref_from) if ref_from in references else 0
		hi = references.index(ref_to) if ref_to in references else len(references) - 1
		allowed = set(references[lo : hi + 1])
		slips = [s for s in slips if s["reference_no"] in allowed]

	# Employer-paid shares (e.g. SS_Com/Med_Com) are booked as an Earning
	# offset by a "<earning> Payable" Deduction so Net Pay is unchanged. They
	# aren't wages, so both legs are left out of Gross Pay / Total Deductions
	# and the deduction columns. Detected by that pairing, no hardcoded names.
	# One column per remaining deduction Salary Component actually used in
	# range, so the columns add up exactly to Total Deductions.
	deductions = []
	for row in slips:
		row["deductions"] = {}
	if slips:
		by_slip = {s.name: s for s in slips}
		details = frappe.get_all(
			"Salary Detail",
			filters={"parent": ["in", list(by_slip)]},
			fields=["parent", "parentfield", "salary_component", "amount"],
			order_by="idx asc",
		)
		slip_deductions = {(d.parent, d.salary_component) for d in details if d.parentfield == "deductions"}
		slip_earnings = {(d.parent, d.salary_component) for d in details if d.parentfield == "earnings"}
		for d in details:
			slip = by_slip[d.parent]
			if d.parentfield == "earnings":
				if (d.parent, f"{d.salary_component} Payable") in slip_deductions:
					slip["gross_pay"] = flt(slip["gross_pay"]) - flt(d.amount)
			elif d.parentfield == "deductions":
				base = d.salary_component.removesuffix(" Payable")
				if base != d.salary_component and (d.parent, base) in slip_earnings:
					slip["total_deduction"] = flt(slip["total_deduction"]) - flt(d.amount)
					continue
				ded = slip["deductions"]
				ded[d.salary_component] = ded.get(d.salary_component, 0) + flt(d.amount)
				if d.salary_component not in deductions:
					deductions.append(d.salary_component)

	totals = {key: sum(flt(row[key]) for row in slips) for key in ("gross_pay", "total_deduction", "net_pay")}
	totals["deductions"] = {c: sum(row["deductions"].get(c, 0) for row in slips) for c in deductions}
	totals["components"] = deductions
	totals["references"] = references
	return slips, totals


def _get_bank_entry_details(payroll_entries: set[str]) -> dict[str, dict]:
	"""Cheque/Reference No + Custom Remarks from each Payroll Entry's cash-
	disbursement Journal Entry (voucher_type Bank/Cash Entry) — same document
	Express Payroll's own reference_no/remarks fields (see [[forms-express-payroll]])
	are written to. Not Salary Slip.journal_entry, which only ever holds the
	*accrual* entry — same reasoning as payroll.py's own _get_payroll_bank_entry().
	Batched into one query (not per-row) since a date range here can cover
	dozens of slips/Payroll Entries at once.
	"""
	if not payroll_entries:
		return {}

	je = frappe.qb.DocType("Journal Entry")
	jea = frappe.qb.DocType("Journal Entry Account")
	rows = (
		frappe.qb.from_(je)
		.inner_join(jea)
		.on(je.name == jea.parent)
		.select(jea.reference_name, je.cheque_no, je.remark, je.custom_remark)
		.where(
			((je.voucher_type == "Bank Entry") | (je.voucher_type == "Cash Entry"))
			& (jea.reference_type == "Payroll Entry")
			& (jea.reference_name.isin(list(payroll_entries)))
			& (je.docstatus == 1)
		)
	).run(as_dict=True)

	return {row.reference_name: row for row in rows}


def _short_date(value) -> str:
	"""2026-09-01 -> "09/01/2026"."""
	d = frappe.utils.getdate(value) if value else None
	return d.strftime("%m/%d/%Y") if d else ""


def _long_date(value) -> str:
	"""2026-09-01 -> "Sept 1, 2026"."""
	d = frappe.utils.getdate(value) if value else None
	if not d:
		return ""
	month = "Sept" if d.month == 9 else d.strftime("%b")
	return f"{month} {d.day}, {d.year}"


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "payrollsummary"
	context.title = _("Payroll Summary")

	company = get_selected_company()
	context.company = company
	context.short_date = _short_date
	context.long_date = _long_date

	from_date, to_date = get_date_range_with_default()
	context.from_date = from_date
	context.to_date = to_date

	quick = get_quick_ranges(from_date, to_date)
	context.this_month_from, context.this_month_to = quick["this_month"]
	context.last_month_from, context.last_month_to = quick["last_month"]
	context.quick_active = quick["active"]

	reference = (frappe.form_dict.get("reference") or "").strip()
	context.reference = reference
	context.ref_from = (frappe.form_dict.get("ref_from") or "").strip()
	context.ref_to = (frappe.form_dict.get("ref_to") or "").strip()

	slips, totals = _build_summary(company, from_date, to_date, reference, context.ref_from, context.ref_to)
	context.slips = slips
	context.totals = totals
	return context


@frappe.whitelist()
def download_excel():
	ensure_logged_in()

	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	from_date, to_date = get_date_range_with_default()
	reference = (frappe.form_dict.get("reference") or "").strip()
	ref_from = (frappe.form_dict.get("ref_from") or "").strip()
	ref_to = (frappe.form_dict.get("ref_to") or "").strip()
	slips, totals = _build_summary(company, from_date, to_date, reference, ref_from, ref_to)

	header = [
		_("Date"),
		_("Employee"),
		_("Reference"),
		_("Start Date"),
		_("End Date"),
		_("Days Worked"),
		_("Frequency"),
		_("Gross Pay"),
		*totals["components"],
		_("Total Deductions"),
		_("Net Pay"),
	]
	data = [header]
	for row in slips:
		data.append(
			[
				row["posting_date"],
				row["employee_name"] or row["employee"],
				row["reference_no"] or "",
				row["start_date"],
				row["end_date"],
				row["payment_days"],
				row["payroll_frequency"],
				row["gross_pay"],
				*[row["deductions"].get(c, 0) for c in totals["components"]],
				row["total_deduction"],
				row["net_pay"],
			]
		)
	data.append(
		[
			"",
			_("All Employees"),
			"",
			"",
			"",
			"",
			"",
			totals["gross_pay"],
			*[totals["deductions"][c] for c in totals["components"]],
			totals["total_deduction"],
			totals["net_pay"],
		]
	)

	build_xlsx_response(data, f"Payroll Summary {from_date} to {to_date}")

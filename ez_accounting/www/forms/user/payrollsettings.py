# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.utils import getdate, nowdate

# Reused directly rather than re-implemented — this is the exact function
# HRMS's own "Bulk Salary Structure Assignment" Desk tool calls per employee
# (hrms.payroll.doctype.bulk_salary_structure_assignment). It resolves
# Payroll Payable Account from Company Defaults when not given, validates the
# account/Income Tax Slab currency, and does the insert()+submit() itself —
# staying in sync with however HRMS builds one on the next upgrade, same
# reasoning as payroll.py's own reuse of get_payment_entry()/
# get_start_end_dates().
from hrms.payroll.doctype.payroll_entry.payroll_entry import get_start_end_dates
from hrms.payroll.doctype.salary_structure.salary_structure import (
	create_salary_structure_assignment,
	get_existing_assignments,
)

from ez_accounting.api import apply_permitted_filter, ensure_admin, get_selected_company

# Reused rather than duplicated — this is the exact auto-provisioning helper
# payroll.py's own _resolve_payroll_period() calls (see forms-express-payroll
# memory: Payroll Entry resolves an employee's Holiday List via a submitted
# "Holiday List Assignment", not Employee.holiday_list, and a missing one
# fails confusingly deep inside HRMS). Cross-page `ez_accounting.www.*` import, same
# precedent as purchase_import.py importing from purchase.py.
from ez_accounting.www.forms.user.payroll import _ensure_holiday_list_assignment

no_cache = 1


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "payrollsettings"
	context.title = _("Payroll Settings")
	context.company = get_selected_company()
	return context


@frappe.whitelist()
def get_salary_structures():
	"""Submitted (docstatus=1), active Salary Structures for the selected
	company only — a batch assignment made through this page can never point
	an employee at a still-Draft structure, the same class of bug already
	found and fixed in payroll.py's own _resolve_payroll_period() (see
	forms-express-payroll memory: a submitted Assignment pointing at a Draft
	Structure fails confusingly deep inside HRMS's own Salary Slip creation,
	not here).
	"""
	ensure_admin()

	company = get_selected_company()
	if not company:
		return []

	return frappe.get_all(
		"Salary Structure",
		filters={"company": company, "docstatus": 1, "is_active": "Yes"},
		fields=["name", "payroll_frequency"],
		order_by="name asc",
	)


@frappe.whitelist()
def get_employees_for_batch():
	"""Every Active employee for the selected company, with their current
	(latest by from_date, if any) Salary Structure Assignment — so the admin
	can see at a glance who already has one, and whether it points at a
	submitted or still-Draft structure, before deciding who to include in a
	batch. Nobody is excluded from the list — re-assigning someone who
	already has an assignment is a legitimate use (e.g. a new structure
	effective from a later date, or fixing one that pointed at a Draft
	structure).
	"""
	ensure_admin()

	company = get_selected_company()
	if not company:
		return []

	employees = frappe.get_all(
		"Employee",
		filters=apply_permitted_filter({"company": company, "status": "Active"}, "Employee"),
		fields=["name", "employee_name"],
		order_by="employee_name asc",
	)
	if not employees:
		return employees

	assignments = frappe.get_all(
		"Salary Structure Assignment",
		filters={"employee": ["in", [e.name for e in employees]], "docstatus": 1},
		fields=["employee", "salary_structure", "from_date"],
		order_by="employee asc, from_date desc",
	)
	latest_by_employee = {}
	for a in assignments:
		latest_by_employee.setdefault(a.employee, a)  # first hit per employee = latest from_date

	structure_names = {a.salary_structure for a in latest_by_employee.values()}
	structure_docstatus = {}
	if structure_names:
		rows = frappe.get_all(
			"Salary Structure", filters={"name": ["in", list(structure_names)]}, fields=["name", "docstatus"]
		)
		structure_docstatus = {r.name: r.docstatus for r in rows}

	for emp in employees:
		latest = latest_by_employee.get(emp.name)
		if latest:
			emp["current_salary_structure"] = latest.salary_structure
			emp["current_from_date"] = latest.from_date
			emp["current_structure_submitted"] = structure_docstatus.get(latest.salary_structure) == 1
		else:
			emp["current_salary_structure"] = None
			emp["current_from_date"] = None
			emp["current_structure_submitted"] = None

	# Preview only — each employee's own current Holiday List, resolved the
	# same two-level way get_holiday_list_for_employee() does (employee-level
	# assignment, falling back to the company-level one), as of *today* — the
	# actual per-run check (against the real pay period, not just today) is
	# what batch_assign_salary_structure() below performs when an employee is
	# actually included in a batch. Company-level assignment is fetched once,
	# not per employee, since it's the same one row for everyone here.
	today = nowdate()
	employee_names = [e.name for e in employees]
	hla_rows = frappe.get_all(
		"Holiday List Assignment",
		filters={"assigned_to": ["in", employee_names + [company]], "docstatus": 1, "from_date": ["<=", today]},
		fields=["assigned_to", "holiday_list", "from_date"],
		order_by="assigned_to asc, from_date desc",
	)
	latest_by_assigned_to = {}
	for row in hla_rows:
		latest_by_assigned_to.setdefault(row.assigned_to, row)  # first hit per assigned_to = latest from_date
	company_holiday_list = latest_by_assigned_to.get(company)

	for emp in employees:
		own = latest_by_assigned_to.get(emp.name)
		resolved = own or company_holiday_list
		emp["current_holiday_list"] = resolved.holiday_list if resolved else None
		emp["has_holiday_list"] = resolved is not None

	return employees


@frappe.whitelist()
def batch_assign_salary_structure(salary_structure: str, from_date: str, employees: str | list):
	"""Create+submit a Salary Structure Assignment for every selected
	employee in one step. One employee failing (e.g. a from_date that
	conflicts with an existing assignment) doesn't abort the rest — each
	gets its own savepoint, rolled back individually on failure, same shape
	as HRMS's own bulk tool (hrms.payroll.doctype.bulk_salary_structure_
	assignment._bulk_assign_structure) — so a genuinely bad row is reported
	back by name instead of silently blocking everyone else in the batch.
	Employees who already have this exact structure/date/company assigned
	and submitted are skipped (not re-created, not counted as a failure).
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	structure_doc = frappe.db.get_value(
		"Salary Structure", salary_structure, ["name", "company", "docstatus", "payroll_frequency"], as_dict=True
	)
	if not structure_doc or structure_doc.company != company:
		frappe.throw(_("{0} is not a valid Salary Structure for {1}.").format(salary_structure, company))
	if structure_doc.docstatus != 1:
		frappe.throw(_("{0} is not submitted — submit it in Desk first.").format(salary_structure))

	if isinstance(employees, str):
		try:
			employees = json.loads(employees)
		except (TypeError, ValueError):
			frappe.throw(_("Invalid employees payload."))
	if not employees or not isinstance(employees, list):
		frappe.throw(_("Select at least one employee."))

	from_date = getdate(from_date)
	currency = frappe.get_cached_value("Company", company, "default_currency")

	already_assigned = set(get_existing_assignments(employees, structure_doc, from_date))
	dates = get_start_end_dates(structure_doc.payroll_frequency, from_date, company)

	success, skipped, failed, holiday_list_assigned = [], [], [], []
	savepoint = "before_batch_salary_assignment"

	for employee in employees:
		employee_doc = frappe.db.get_value(
			"Employee", employee, ["name", "employee_name", "company", "status"], as_dict=True
		)
		if not employee_doc or employee_doc.company != company:
			failed.append({"employee": employee, "employee_name": employee, "error": _("Not a valid employee for {0}.").format(company)})
			continue
		if employee_doc.status != "Active":
			failed.append(
				{
					"employee": employee,
					"employee_name": employee_doc.employee_name,
					"error": _("{0} is not an active employee.").format(employee_doc.employee_name),
				}
			)
			continue
		if employee in already_assigned:
			skipped.append({"employee": employee, "employee_name": employee_doc.employee_name})
			_ensure_holiday_list_for_batch_row(employee, employee_doc, company, dates, holiday_list_assigned, failed)
			continue

		frappe.db.savepoint(savepoint)
		try:
			assignment_name = create_salary_structure_assignment(
				employee=employee,
				salary_structure=salary_structure,
				company=company,
				currency=currency,
				from_date=from_date,
			)
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			failed.append({"employee": employee, "employee_name": employee_doc.employee_name, "error": str(e)})
		else:
			success.append(
				{"employee": employee, "employee_name": employee_doc.employee_name, "assignment": assignment_name}
			)
			_ensure_holiday_list_for_batch_row(employee, employee_doc, company, dates, holiday_list_assigned, failed)

	frappe.db.commit()

	return {
		"success": success,
		"skipped": skipped,
		"failed": failed,
		"holiday_list_assigned": holiday_list_assigned,
	}


def _ensure_holiday_list_for_batch_row(employee, employee_doc, company, dates, holiday_list_assigned, failed):
	"""Auto-assigns a Holiday List alongside the Salary Structure — same
	instinct as the batch's own Salary Structure Assignment: an employee
	selected here should come out of this page fully payroll-ready, not just
	one step closer. Its own savepoint since a failure here (only possible if
	the company has zero Holiday Lists at all — see
	_ensure_holiday_list_assignment's own docstring) shouldn't undo the
	Salary Structure Assignment that already succeeded/was confirmed above.
	"""
	savepoint = "before_batch_holiday_list_assignment"
	frappe.db.savepoint(savepoint)
	try:
		created = _ensure_holiday_list_assignment(employee, company, dates.start_date, dates.end_date)
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		failed.append(
			{
				"employee": employee,
				"employee_name": employee_doc.employee_name,
				"error": _("Salary Structure assigned, but Holiday List could not be: {0}").format(str(e)),
			}
		)
	else:
		if created:
			holiday_list_assigned.append({"employee": employee, "employee_name": employee_doc.employee_name})


@frappe.whitelist()
def batch_assign_holiday_list(holiday_list: str, from_date: str, employees: str | list):
	"""Standalone counterpart to batch_assign_salary_structure()'s own
	auto-provisioning above — for assigning/changing an employee's Holiday
	List directly, without needing to also (re-)assign a Salary Structure
	just to trigger it. Same per-employee savepoint shape; skips only an
	*exact* duplicate (employee + this Holiday List + this From Date, already
	submitted) — deliberately not "employee already has any Holiday List",
	since picking a different list or a different effective date here is a
	legitimate, common reason to use this tool.
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	if not frappe.db.exists("Holiday List", holiday_list):
		frappe.throw(_("{0} is not a valid Holiday List.").format(holiday_list))

	if isinstance(employees, str):
		try:
			employees = json.loads(employees)
		except (TypeError, ValueError):
			frappe.throw(_("Invalid employees payload."))
	if not employees or not isinstance(employees, list):
		frappe.throw(_("Select at least one employee."))

	from_date = getdate(from_date)

	success, skipped, failed = [], [], []
	savepoint = "before_batch_holiday_list_assignment_standalone"

	for employee in employees:
		employee_doc = frappe.db.get_value(
			"Employee", employee, ["name", "employee_name", "company", "status"], as_dict=True
		)
		if not employee_doc or employee_doc.company != company:
			failed.append({"employee": employee, "employee_name": employee, "error": _("Not a valid employee for {0}.").format(company)})
			continue
		if employee_doc.status != "Active":
			failed.append(
				{
					"employee": employee,
					"employee_name": employee_doc.employee_name,
					"error": _("{0} is not an active employee.").format(employee_doc.employee_name),
				}
			)
			continue

		already = frappe.db.exists(
			"Holiday List Assignment",
			{"assigned_to": employee, "holiday_list": holiday_list, "from_date": from_date, "docstatus": 1},
		)
		if already:
			skipped.append({"employee": employee, "employee_name": employee_doc.employee_name})
			continue

		frappe.db.savepoint(savepoint)
		try:
			hla = frappe.get_doc(
				{
					"doctype": "Holiday List Assignment",
					"applicable_for": "Employee",
					"assigned_to": employee,
					"holiday_list": holiday_list,
					"from_date": from_date,
				}
			)
			hla.insert()
			hla.submit()
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			failed.append({"employee": employee, "employee_name": employee_doc.employee_name, "error": str(e)})
		else:
			success.append(
				{"employee": employee, "employee_name": employee_doc.employee_name, "assignment": hla.name}
			)

	frappe.db.commit()

	return {"success": success, "skipped": skipped, "failed": failed}

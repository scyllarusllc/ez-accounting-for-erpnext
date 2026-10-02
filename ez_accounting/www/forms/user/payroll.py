# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import json
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.query_builder.functions import Count, Max, Min, Sum
from frappe.utils import flt, nowdate, strip_html

# HRMS's own period-boundary logic: given a frequency + a reference date, snaps to
# the real period (e.g. Monthly always resolves to the containing calendar month,
# Bimonthly to the 1st-15th/16th-end half). Reusing it — instead of reimplementing
# it here — keeps this in sync with whatever payroll formulas (e.g. this site's
# Ch_2 CNMI withholding formula) assume about period length.
from hrms.payroll.doctype.payroll_entry.payroll_entry import get_start_end_dates
from hrms.utils.holiday_list import get_holiday_list_for_employee

from ez_accounting.api import ensure_admin, get_selected_company, is_company_restricted, permitted_docs
from ez_accounting.api import get_company_bank_accounts as _get_company_bank_accounts

no_cache = 1

# Confirmed against every active Salary Structure on this site (Salary Structure 1,
# Salary Structure With CH7, Salary Structure with CH7 - 2, Semi-monthly, test,
# Test Monthly Salary Structure - EIU): "Regular" is the one earning component with
# amount_based_on_formula=0 (a static amount, default 0 on the structure itself).
# Every other earning/deduction (SS_Com, Med_Com, Ch_2, SS_ee, MED_ee, SS_Com Payable,
# Med_Com Payable) is a formula driven off "Regular"'s abbreviation. So "Regular" is
# the one component a payroll run is expected to set per employee per period.
# Revisit this constant if a new Salary Structure changes that convention.
BASE_PAY_COMPONENT = "Regular"

PAY_MODES = {"Hourly", "Regular Gross"}

# HRMS emits this as a blue notice even when get_tax_components() returns an
# empty list. These structures intentionally calculate CNMI withholding through
# the formula-based Ch_2 component, so the notice is both inaccurate and looks
# like a payroll error in this portal's response dialog.
HRMS_EMPTY_TAX_COMPONENT_NOTICE = _(
	"Added tax components from the Salary Component master as the salary structure didn't have any tax component."
)


def _discard_empty_tax_component_notice() -> None:
	message_log = getattr(frappe.local, "message_log", None)
	if not message_log:
		return
	frappe.local.message_log = [
		entry
		for entry in message_log
		if strip_html(str(entry.get("message", ""))) != HRMS_EMPTY_TAX_COMPONENT_NOTICE
	]


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "payroll"
	context.title = _("Express Payroll")
	context.company = get_selected_company()
	return context


@frappe.whitelist()
def get_active_employees():
	"""Only employees with an active Salary Structure Assignment can actually be run
	through Express Payroll — _resolve_payroll_period() throws otherwise. Filtering
	here (rather than listing every Active employee) keeps the dropdown from being
	full of choices that are guaranteed to dead-end in an error after the admin has
	already picked a date.
	"""
	ensure_admin()
	payroll_ready = frappe.get_all(
		"Salary Structure Assignment", filters={"docstatus": 1}, pluck="employee", distinct=True
	)
	if not payroll_ready:
		return []
	# Intersect with the Employees this user is permitted (User Permission),
	# before it becomes the `name in (...)` filter below.
	permitted_employees = permitted_docs("Employee")
	if permitted_employees is not None:
		allowed = set(permitted_employees)
		payroll_ready = [e for e in payroll_ready if e in allowed]
		if not payroll_ready:
			return []
	filters = {"status": "Active", "name": ["in", payroll_ready]}
	company = get_selected_company()
	if company:
		filters["company"] = company
	elif is_company_restricted():
		return []
	return frappe.get_all(
		"Employee",
		filters=filters,
		fields=["name", "employee_name", "company"],
		order_by="employee_name asc",
	)


@frappe.whitelist()
def get_salary_slip_history():
	"""Recent submitted Salary Slips so the admin can see what's already been run
	before starting a new one, without cancelled transactions cluttering the list.
	"""
	ensure_admin()
	filters = {"docstatus": 1}
	company = get_selected_company()
	if company:
		filters["company"] = company
	elif is_company_restricted():
		return []
	permitted_employees = permitted_docs("Employee")
	if permitted_employees is not None:
		filters["employee"] = ["in", permitted_employees]
	slips = frappe.get_all(
		"Salary Slip",
		filters=filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"start_date",
			"end_date",
			"payroll_frequency",
			"gross_pay",
			"net_pay",
			"currency",
			"status",
			"journal_entry",
			"payroll_entry",
			"creation",
			"owner",
		],
		order_by="end_date desc, creation desc",
		limit_page_length=50,
	)

	# owner is the creating user's email/ID, not something an admin wants to
	# read at a glance — resolved to Full Name in one batch query rather than
	# per-row. Mirrors purchase.py's/sales.py's get_*_invoice_history().
	full_name_by_owner = {}
	owners = {slip.owner for slip in slips if slip.owner}
	if owners:
		owner_rows = frappe.get_all("User", filters={"name": ["in", list(owners)]}, fields=["name", "full_name"])
		full_name_by_owner = {row.name: row.full_name for row in owner_rows}

	check_number_by_payroll_entry = _get_payroll_check_numbers(
		{slip.payroll_entry for slip in slips if slip.payroll_entry}
	)

	for slip in slips:
		slip["created_by"] = full_name_by_owner.get(slip.owner) or slip.owner
		slip["check_number"] = check_number_by_payroll_entry.get(slip.payroll_entry)

	return slips


@frappe.whitelist()
def get_company_payroll_summary():
	"""Submitted payroll totals for every company the user may access.

	This deliberately summarizes Salary Slips, not General Ledger receipts:
	``net_pay`` is the amount paid to employees, while ``gross_pay`` provides
	the before-deduction comparison. Cancelled and draft slips are excluded.
	"""
	ensure_admin()
	permitted_companies = permitted_docs("Company")
	if permitted_companies is not None:
		companies = [company for company in permitted_companies if frappe.db.exists("Company", company)]
		if not companies:
			return []
	else:
		companies = frappe.get_all("Company", pluck="name", ignore_permissions=True)

	salary_slip = frappe.qb.DocType("Salary Slip")
	rows = (
		frappe.qb.from_(salary_slip)
		.select(
			salary_slip.company,
			salary_slip.currency,
			Count(salary_slip.name).as_("salary_slip_count"),
			Sum(salary_slip.gross_pay).as_("gross_pay"),
			Sum(salary_slip.net_pay).as_("net_pay"),
			Min(salary_slip.start_date).as_("first_start_date"),
			Max(salary_slip.end_date).as_("last_end_date"),
		)
		.where((salary_slip.docstatus == 1) & (salary_slip.company.isin(companies)))
		.groupby(salary_slip.company, salary_slip.currency)
		.orderby(salary_slip.company, salary_slip.currency)
	).run(as_dict=True)

	for row in rows:
		row["salary_slip_count"] = int(row.salary_slip_count or 0)
		# Match currency posting/display semantics. Python's ordinary float
		# rounding is bankers' rounding (e.g. 44,224.085 -> 44,224.08), while
		# accounting amounts use half-up rounding to cents.
		row["gross_pay"] = float(Decimal(str(row.gross_pay or 0)).quantize(Decimal("0.01"), ROUND_HALF_UP))
		row["net_pay"] = float(Decimal(str(row.net_pay or 0)).quantize(Decimal("0.01"), ROUND_HALF_UP))
	return rows


def _get_payroll_check_numbers(payroll_entries: set[str]) -> dict[str, str]:
	"""Return the bank/cash Journal Entry check number for each payroll run.

	Only submitted entries are relevant because Salary Slip History excludes
	cancelled payroll.
	"""
	if not payroll_entries:
		return {}

	je = frappe.qb.DocType("Journal Entry")
	jea = frappe.qb.DocType("Journal Entry Account")
	rows = (
		frappe.qb.from_(je)
		.inner_join(jea)
		.on(je.name == jea.parent)
		.select(jea.reference_name, je.cheque_no)
		.where(
			((je.voucher_type == "Bank Entry") | (je.voucher_type == "Cash Entry"))
			& (jea.reference_type == "Payroll Entry")
			& (jea.reference_name.isin(list(payroll_entries)))
			& (je.docstatus == 1)
		)
	).run(as_dict=True)

	check_numbers = {}
	for row in rows:
		if row.reference_name not in check_numbers:
			check_numbers[row.reference_name] = row.cheque_no
	return check_numbers


@frappe.whitelist()
def get_company_bank_accounts():
	ensure_admin()
	company = get_selected_company()
	if company:
		_ensure_default_bank_account(company)
	return _get_company_bank_accounts(company)


def _ensure_default_bank_account(company: str) -> None:
	"""Auto-provisions one company Bank Account for `company` if it doesn't
	have a usable one yet — otherwise this page's own Bank Account dropdown
	is stuck permanently empty for any company that was only ever set up
	with a Chart of Accounts (common for the less-active companies on this
	multi-company bench) and never had a Bank Account record added by hand in
	Desk. Uses the first Bank-type GL Account under that company, in the same
	order the tree at /desk/account/view/tree shows them (nested-set `lft`,
	ascending) — "the first bank account" the admin would see there. A no-op
	whenever there's nothing sensible to create from (no Bank-type account at
	all for this company), or when a usable one already exists.

	Deliberately swallows any failure here (logged, not raised) — this is a
	convenience auto-fill for a read endpoint called on every page load, not
	something that should ever turn a payroll run's bank-account dropdown
	into a hard error. The admin can always still create one by hand in Desk
	regardless of whether this succeeds.
	"""
	if frappe.db.exists("Bank Account", {"company": company, "is_company_account": 1, "disabled": 0}):
		return

	account = frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Bank", "is_group": 0},
		"name",
		order_by="lft asc",
	)
	if not account:
		return

	try:
		# Bank Account.account is unique in practice (validate_account()
		# throws if another Bank Account already claims it) — if one already
		# does, for whatever reason (e.g. disabled, or never marked as a
		# company account), fix that one up instead of trying to create a
		# second record against the same GL account and hitting that error.
		existing_name = frappe.db.get_value("Bank Account", {"account": account})
		if existing_name:
			bank_account = frappe.get_doc("Bank Account", existing_name)
			bank_account.company = company
			bank_account.is_company_account = 1
			bank_account.disabled = 0
			bank_account.save(ignore_permissions=True)
			frappe.db.commit()
			return

		# Bank (the institution, e.g. "Bank of Hawaii") is a mandatory field
		# on Bank Account but has no bearing on GL postings at all — only
		# .account does. Best-effort matched against the GL account's own
		# name (most of this site's account names already embed the bank,
		# e.g. "...-FHB 9777 Reg-...", "...- BOH 3054 Reg -..."), falling
		# back to whatever Bank record happens to exist first so this never
		# blocks on master data the admin hasn't set up — the institution
		# label can always be corrected by hand in Desk afterward.
		bank_names = frappe.get_all("Bank", pluck="name", order_by="name asc")
		if not bank_names:
			return
		bank = next((b for b in bank_names if b.lower() in account.lower()), bank_names[0])

		bank_account = frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": account,
				"account": account,
				"bank": bank,
				"company": company,
				"is_company_account": 1,
			}
		)
		bank_account.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Payroll default Bank Account creation failed")


def _resolve_payroll_period(employee: str, company: str, start_date: str) -> dict:
	"""Look up the employee's active Salary Structure Assignment as of start_date and
	derive the actual payroll period from it — the portal only asks for a Start Date;
	Payroll Frequency and End Date are never taken from the client, always recomputed
	here from the employee's own assignment so they can't drift out of sync with it.
	"""
	assignment = frappe.db.get_value(
		"Salary Structure Assignment",
		{"employee": employee, "docstatus": 1, "from_date": ["<=", start_date]},
		["name", "salary_structure", "payroll_payable_account", "currency"],
		order_by="from_date desc",
		as_dict=True,
	)
	if not assignment:
		frappe.throw(
			_("No active Salary Structure Assignment found for employee {0} as of {1}.").format(
				employee, start_date
			)
		)
	if not assignment.payroll_payable_account:
		frappe.throw(
			_("The Salary Structure Assignment for {0} has no Payroll Payable Account set.").format(employee)
		)

	structure = frappe.db.get_value(
		"Salary Structure", assignment.salary_structure, ["payroll_frequency", "docstatus"], as_dict=True
	)
	# A Salary Structure Assignment can be submitted (docstatus=1) while the
	# Salary Structure it points to is still a Draft — HRMS only catches this
	# much later, deep inside Payroll Entry.create_salary_slips(), with a
	# confusing "Please assign a Salary Structure ... applicable from or
	# before <date> first" error that surfaces alongside a second, unrelated-
	# looking "Payroll Entry ... not found" (this function's own db_set/
	# reload calls on a Payroll Entry that process_express_payroll()'s own
	# except-block has by then already rolled back). Checked here instead so
	# this fails immediately, before anything is created at all, with a
	# message that actually names the real problem.
	if not structure or structure.docstatus != 1:
		frappe.throw(
			_(
				"Salary Structure {0} (assigned to {1}) is not submitted — a Salary Slip can't be"
				" generated from a draft Salary Structure. Submit it in Desk first, then try again."
			).format(assignment.salary_structure, employee)
		)
	payroll_frequency = structure.payroll_frequency
	if not payroll_frequency:
		frappe.throw(
			_("Salary Structure {0} has no Payroll Frequency set.").format(assignment.salary_structure)
		)

	dates = get_start_end_dates(payroll_frequency, start_date, company)
	assignment.payroll_frequency = payroll_frequency
	assignment.start_date = dates.start_date
	assignment.end_date = dates.end_date

	# HRMS resolves the employee's Holiday List for payroll purposes via a
	# submitted "Holiday List Assignment" (assigned_to the employee, or their
	# company as a fallback) covering the period — NOT the plain
	# Employee.holiday_list field (that's read elsewhere, e.g. LWP/attendance,
	# but not by Payroll Entry's own salary-slip creation). Missing this only
	# ever surfaced deep inside HRMS's own create_salary_slips_for_employees(),
	# which catches the real "No Holiday List was found..." exception internally
	# and then calls self.reload() on the Payroll Entry — which by then has
	# already been rolled back along with everything else in this failed
	# request — producing an unrelated-looking "Payroll Entry ... not found"
	# with no hint of the actual cause. Auto-provisioned here instead, before
	# anything else is created, same instinct as _ensure_default_bank_account()
	# below (a real, previously-common onboarding gap shouldn't need a trip to
	# Desk every time it's hit).
	_ensure_holiday_list_assignment(employee, company, dates.start_date, dates.end_date)

	return assignment


def _ensure_holiday_list_assignment(employee: str, company: str, start_date, end_date) -> bool:
	"""Auto-creates a submitted Holiday List Assignment for `employee` if the
	one HRMS actually reads for payroll (get_holiday_list_for_employee(), see
	the caller's own comment) doesn't already cover this pay period. Returns
	whether a new one was actually created (False if one already covered the
	period, or True once one's created here) — payrollsettings.py's own batch
	tool uses this to report who got a new one alongside the Salary Structure
	Assignment it just created.

	Reuses employee.py's own _resolve_default_holiday_list() — Company.
	default_holiday_list, falling back to the sole Holiday List on the site —
	rather than reinventing that fallback here; this is exactly the same
	default the Employee page already backfills onto Employee.holiday_list,
	just written to the doctype Payroll Entry itself actually consults.
	Throws (naming the real problem) only if no default can be resolved at
	all, e.g. a company with zero Holiday Lists to fall back to — nothing
	sensible to auto-assign in that case.
	"""
	if get_holiday_list_for_employee(
		employee, as_on=start_date, raise_exception=False
	) and get_holiday_list_for_employee(employee, as_on=end_date, raise_exception=False):
		return False

	from ez_accounting.www.forms.user.employee import _resolve_default_holiday_list

	default_list = _resolve_default_holiday_list(company)
	if not default_list:
		frappe.throw(
			_(
				"No Holiday List covers {0}'s pay period ({1} → {2}) for {3}, and no default Holiday"
				" List could be resolved to auto-assign one — create a Holiday List for {3} first."
			).format(employee, start_date, end_date, company)
		)

	hl_from_date = frappe.db.get_value("Holiday List", default_list, "from_date") or start_date
	hla = frappe.get_doc(
		{
			"doctype": "Holiday List Assignment",
			"applicable_for": "Employee",
			"assigned_to": employee,
			"holiday_list": default_list,
			"from_date": hl_from_date,
		}
	)
	hla.insert(ignore_permissions=True)
	hla.submit()
	return True


@frappe.whitelist()
def get_payroll_period(employee: str, start_date: str):
	"""Preview endpoint for the form: given an employee + a start date, resolve and
	return the actual pay period (frequency, snapped start/end dates) so the portal
	can show it before the run, without letting the client set it directly.
	"""
	ensure_admin()

	if not employee or not start_date:
		frappe.throw(_("Employee and Start Date are required."))

	company = frappe.db.get_value("Employee", employee, "company")
	if not company:
		frappe.throw(_("Employee {0} not found.").format(employee))

	period = _resolve_payroll_period(employee, company, start_date)
	return {
		"payroll_frequency": period.payroll_frequency,
		"start_date": period.start_date,
		"end_date": period.end_date,
	}


@frappe.whitelist()
def get_deduction_components():
	"""Active Deduction-type Salary Components, for the payroll form's own
	one-off deduction-line picker (see process_express_payroll()'s
	"deductions" step) — e.g. a loan repayment, a uniform charge, or a
	school-fee withholding, added on top of whatever the Salary Structure
	itself already computes.
	"""
	ensure_admin()
	return frappe.get_all(
		"Salary Component",
		filters={"type": "Deduction", "disabled": 0},
		fields=["name"],
		order_by="name asc",
	)


def _parse_deduction_lines(deductions: str | list) -> list[dict]:
	"""Parse+validate the Salary Component/Amount rows for one-off deductions
	added to this payroll run — see process_express_payroll()'s own
	docstring for where these land (appended directly to the already-created
	draft Salary Slip, not via an Additional Salary). Every component must
	be an active, non-disabled Deduction-type Salary Component; duplicates
	within the same request are rejected outright rather than silently
	combined into one row.
	"""
	if isinstance(deductions, str):
		try:
			deductions = json.loads(deductions) if deductions else []
		except (TypeError, ValueError):
			frappe.throw(_("Invalid deductions payload."))
	deductions = deductions or []

	parsed = []
	seen = set()
	for row in deductions:
		salary_component = (row.get("salary_component") or "").strip()
		amount = flt(row.get("amount"))
		if not salary_component:
			continue
		if amount <= 0:
			frappe.throw(_("Amount must be greater than zero for deduction {0}.").format(salary_component))
		if salary_component in seen:
			frappe.throw(_("{0} was added more than once — combine it into a single row.").format(salary_component))
		seen.add(salary_component)

		component_doc = frappe.db.get_value(
			"Salary Component",
			salary_component,
			["name", "type", "disabled", "salary_component_abbr"],
			as_dict=True,
		)
		if not component_doc or component_doc.disabled or component_doc.type != "Deduction":
			frappe.throw(_("{0} is not an active Deduction component.").format(salary_component))

		parsed.append(
			{"salary_component": component_doc.name, "abbr": component_doc.salary_component_abbr, "amount": amount}
		)

	return parsed


def _extract_real_payroll_failure(payroll_entry_name: str) -> dict | None:
	"""Recovers the real error behind a masking "Payroll Entry ... not found"
	failure (see this function's own caller, in process_express_payroll()'s
	except frappe.DoesNotExistError block).

	HRMS's create_salary_slips_for_employees()/submit_salary_slips_for_employees()
	each wrap their own per-employee work in a try/except that, on any
	failure, does its own frappe.db.rollback() (undoing this run's own
	just-created Payroll Entry along with whatever else was pending) and then
	calls log_payroll_failure() — which writes a real Error Log entry (title
	"Salary Slip {creation|submission} failed for Payroll Entry {name}") via
	frappe.log_error(), the one artifact that survives that rollback. The
	*clean* message it also tries to db_set() onto the Payroll Entry's own
	error_message field never actually persists — that row is already gone by
	then — so the Error Log's own stored traceback is the only place left to
	recover the real message from. Its last line is always the plain
	"module.path.ExceptionClass: message" Python traceback tail (confirmed
	against real Error Log entries this session — the "Traceback with
	variables" formatting Frappe uses interleaves variable dumps *between*
	frames, but always ends with this one plain line), so that's parsed
	directly rather than trying to regex out an inline "e = ..." variable
	dump line (Frappe's own choice of local variable name there isn't a
	documented contract, this trailing line's format is).
	"""
	log = frappe.db.get_value(
		"Error Log",
		{
			"method": [
				"in",
				[
					_("Salary Slip creation failed for Payroll Entry {0}").format(payroll_entry_name),
					_("Salary Slip submission failed for Payroll Entry {0}").format(payroll_entry_name),
				],
			]
		},
		["name", "error"],
		order_by="creation desc",
		as_dict=True,
	)
	if not log or not log.error:
		return None

	lines = [line for line in log.error.strip().splitlines() if line.strip()]
	if not lines:
		return None
	last_line = lines[-1].strip()
	message = last_line.split(":", 1)[1].strip() if ":" in last_line else last_line
	# HRMS's own messages often wrap parts in frappe.bold()'s <strong> tags —
	# meaningless (and, once this whole string goes through this page's own
	# client-side escape_html() before display, visibly ugly) once it's no
	# longer rendered as real HTML.
	message = strip_html(message)
	return {"message": message, "log_name": log.name}


@frappe.whitelist()
def process_express_payroll(
	employee: str,
	start_date: str,
	pay_mode: str,
	bank_account: str,
	hours_worked: float = 0,
	pay_rate: float = 0,
	gross_amount: float = 0,
	deductions: str | list = None,
	reference_no: str = "",
	remarks: str = "",
):
	"""Run a single-employee payroll cycle end-to-end, following HRMS v16's own
	Payroll Entry flow: submitting the Payroll Entry generates the Salary Slip
	(from the employee's assigned Salary Structure), then (unless "deductions"
	is given) the slip is submitted directly (booking the accrual GL entry),
	and finally the cash disbursement Journal Entry is created and submitted.

	Deliberately drives everything through a single-employee `Payroll Entry`
	rather than submitting a bare Salary Slip: in this HRMS version, GL entries
	for payroll (accrual *and* disbursement) are created by Payroll Entry's own
	methods, not by Salary Slip.on_submit(). Reimplementing that GL math by hand
	here would risk drifting from what HRMS actually posts on the next upgrade.

	`deductions` (optional) is a JSON list of one-off {salary_component, amount}
	rows — e.g. a loan repayment or a school-fee withholding for this one
	period, on top of whatever the Salary Structure itself already computes
	(SS_ee, Ch_2, etc.). Applied directly to the Salary Slip's own
	`deductions` child table, between it being generated (still a Draft) and
	submitted — *not* via an Additional Salary the way the base-pay override
	above works. That mechanism only matters for a component that has to be
	baked in *before* the slip is first generated (a brand new slip has empty
	earnings/deductions tables, so Salary Slip.calculate_net_pay() would
	otherwise recompute "Regular" fresh from the Structure's own static
	default of 0 on every save — see Step 1's own comment); by the time these
	deduction rows are appended, the slip already has its real component rows
	populated from Step 2, so a plain append()+save() survives every later
	recalculation exactly like any other row on the slip does. Confirmed live
	(see forms-express-payroll memory) that a manually-appended row does
	*not* survive if the component has no GL account configured for this
	company (Payroll Entry.make_accrual_jv_entry() fails deep in the flow
	with "Please set account in Salary Component X", and the whole run
	rolls back) — checked up front, before anything is created, same
	fail-fast reasoning as every other validation in this function.
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run payroll."), frappe.PermissionError)

	if pay_mode not in PAY_MODES:
		frappe.throw(_("Invalid pay mode: {0}").format(pay_mode))

	if pay_mode == "Hourly":
		base_amount = flt(hours_worked) * flt(pay_rate)
	else:
		base_amount = flt(gross_amount)

	if base_amount <= 0:
		frappe.throw(_("The calculated pay amount must be greater than zero."))

	deduction_lines = _parse_deduction_lines(deductions)

	employee_doc = frappe.db.get_value(
		"Employee", employee, ["name", "employee_name", "company", "status"], as_dict=True
	)
	if not employee_doc:
		frappe.throw(_("Employee {0} not found.").format(employee))
	if employee_doc.status != "Active":
		frappe.throw(_("{0} is not an active employee.").format(employee_doc.employee_name))

	bank_account_doc = frappe.db.get_value(
		"Bank Account",
		bank_account,
		["name", "account", "company", "is_company_account", "disabled"],
		as_dict=True,
	)
	if not bank_account_doc or not bank_account_doc.is_company_account or bank_account_doc.disabled:
		frappe.throw(_("{0} is not an active company Bank Account.").format(bank_account))
	if not bank_account_doc.account:
		frappe.throw(_("Bank Account {0} is not linked to a GL Account.").format(bank_account))

	company = employee_doc.company
	# get_company_bank_accounts() is now scoped to the selected company, but
	# still kept as a server-side check rather than trusted from the client —
	# e.g. a stale page left open in another tab after switching companies,
	# or an employee list loaded before a company switch took effect.
	if bank_account_doc.company and bank_account_doc.company != company:
		frappe.throw(
			_("{0} belongs to {1}, not {2} ({3}'s company).").format(
				bank_account, bank_account_doc.company, company, employee_doc.employee_name
			)
		)
	# Checked here, before anything is created, rather than letting a missing
	# mapping surface deep inside Payroll Entry.make_accrual_jv_entry() after
	# the Additional Salary/Payroll Entry/Salary Slip have already been
	# created — same "fail fast, name the real problem" reasoning as
	# _resolve_payroll_period()'s own Draft-Structure check above.
	for line in deduction_lines:
		if not frappe.db.exists(
			"Salary Component Account", {"parent": line["salary_component"], "company": company}
		):
			frappe.throw(
				_(
					"{0} has no GL account configured for {1} (Salary Component {0} > Accounts table) —"
					" set one in Desk first."
				).format(line["salary_component"], company)
			)

	assignment = _resolve_payroll_period(employee_doc.name, company, start_date)
	payroll_frequency = assignment.payroll_frequency
	start_date = assignment.start_date
	end_date = assignment.end_date

	# Match Payroll Entry.validate_existing_salary_slips() before creating the
	# base-pay Additional Salary or Payroll Entry. Without this guard, a consumed
	# (disabled) override lets a repeat request reach HRMS, which then fails with
	# an HTML-heavy duplicate message after the new override has already been
	# submitted. A submitted slip means this period is complete; a draft must be
	# resolved in Desk before Express Payroll can safely proceed.
	existing_slip = frappe.db.get_value(
		"Salary Slip",
		{
			"employee": employee_doc.name,
			"start_date": start_date,
			"end_date": end_date,
			"docstatus": ["!=", 2],
		},
		["name", "docstatus", "payroll_entry"],
		order_by="docstatus desc, creation desc",
		as_dict=True,
	)
	if existing_slip:
		if existing_slip.docstatus == 1:
			frappe.throw(
				_(
					"Payroll for {0} is already complete for {1} to {2}: Salary Slip {3}"
					" is submitted under Payroll Entry {4}. Do not run it again."
				).format(
					employee_doc.employee_name,
					start_date,
					end_date,
					existing_slip.name,
					existing_slip.payroll_entry or _("not recorded"),
				)
			)
		frappe.throw(
			_(
				"Draft Salary Slip {0} already exists for {1} from {2} to {3}."
				" Submit or delete that draft in Desk before running Express Payroll."
			).format(existing_slip.name, employee_doc.employee_name, start_date, end_date)
		)

	cost_center = frappe.get_cached_value("Company", company, "cost_center")
	currency = assignment.currency or frappe.get_cached_value("Company", company, "default_currency")

	# create_salary_slips()/submit_salary_slips() (and now Payroll Entry.submit()
	# itself) call frappe.db.commit() internally — HRMS does this for background-
	# job resilience — so a failure in a *later* step can't be undone by this
	# function's own rollback below; whatever already got submitted stays
	# submitted. The most common leftover from a prior failed run is an
	# Additional Salary that succeeded and committed before something downstream
	# failed. Check for it up front and point at the actual blocking document
	# instead of letting HRMS's generic duplicate error surface with no way to
	# tell what's causing it.
	conflicting_additional_salary = frappe.db.get_value(
		"Additional Salary",
		{
			"employee": employee_doc.name,
			"salary_component": BASE_PAY_COMPONENT,
			"overwrite_salary_structure_amount": 1,
			"docstatus": 1,
			"disabled": 0,
			"payroll_date": end_date,
		},
	)
	if conflicting_additional_salary:
		frappe.throw(
			_(
				"{0} already has a base-pay override for {1} on {2} ({3}). This is usually left over from "
				"an earlier run that didn't finish — check whether a Salary Slip for this period already "
				"exists before cancelling it and retrying."
			).format(
				employee_doc.employee_name, BASE_PAY_COMPONENT, end_date, conflicting_additional_salary
			)
		)

	payroll_entry = None

	try:
		# Step 1 — override this period's base pay via Additional Salary, the
		# HRMS-supported way to set a static ("Regular") component's amount for
		# one employee/period. Editing the Salary Slip's own earnings row directly
		# doesn't stick: any recalculation (which insert()/save() always trigger)
		# resets a non-formula component straight back to the Salary Structure's
		# static default (0 here) unless an Additional Salary override exists.
		# This has to happen *before* the Salary Slip is generated below.
		additional_salary = frappe.get_doc(
			{
				"doctype": "Additional Salary",
				"employee": employee_doc.name,
				"company": company,
				"salary_component": BASE_PAY_COMPONENT,
				"type": "Earning",
				"amount": base_amount,
				"currency": currency,
				"payroll_date": end_date,
				"is_recurring": 0,
				"overwrite_salary_structure_amount": 1,
			}
		)
		additional_salary.insert()
		additional_salary.submit()

		# Step 2 — Salary Slip, via a single-employee Payroll Entry. Submitting
		# the Payroll Entry itself (rather than just calling the standalone
		# create_salary_slips() helper) is what generates the Salary Slip in
		# stock HRMS v16 — Payroll Entry.on_submit() calls create_salary_slips()
		# internally, so this leaves an audit trail matching the normal Desk flow.
		payroll_entry = frappe.get_doc(
			{
				"doctype": "Payroll Entry",
				"company": company,
				"posting_date": nowdate(),
				"start_date": start_date,
				"end_date": end_date,
				"payroll_frequency": payroll_frequency,
				"currency": currency,
				"exchange_rate": 1,
				"cost_center": cost_center,
				"payroll_payable_account": assignment.payroll_payable_account,
				"payment_account": bank_account_doc.account,
				"bank_account": bank_account_doc.name,
				"employees": [
					{
						"employee": employee_doc.name,
						"employee_name": employee_doc.employee_name,
					}
				],
			}
		)
		payroll_entry.insert()
		payroll_entry.submit()
		_discard_empty_tax_component_notice()

		slip_name = frappe.db.get_value(
			"Salary Slip", {"payroll_entry": payroll_entry.name, "employee": employee_doc.name}
		)
		if not slip_name:
			frappe.throw(
				_(
					"Salary Slip creation failed for {0}. {1}"
				).format(
					employee_doc.employee_name,
					payroll_entry.error_message
					or _("A Salary Slip may already exist for this employee and period."),
				)
			)

		# Step 2.5 — one-off deductions (see this function's own docstring for
		# why these are appended directly rather than via an Additional
		# Salary): the slip already has real earnings/deductions rows from
		# Step 2, so a plain append()+save() sticks through every later
		# recalculation. Guards against a component the Structure already
		# computes (e.g. manually adding "SS_ee" on top of its own
		# formula-driven row would just be ambiguous, not additive).
		if deduction_lines:
			slip = frappe.get_doc("Salary Slip", slip_name)
			existing_components = {d.salary_component for d in slip.deductions}
			for line in deduction_lines:
				if line["salary_component"] in existing_components:
					frappe.throw(
						_(
							"{0} is already a deduction on this Salary Slip (likely computed automatically"
							" by the Salary Structure) — pick a different component."
						).format(line["salary_component"])
					)
				slip.append(
					"deductions",
					{
						"salary_component": line["salary_component"],
						"abbr": line["abbr"],
						"amount": line["amount"],
						"default_amount": line["amount"],
					},
				)
			slip.save()
			frappe.db.commit()

		# Step 3 — submitting the slip also books the accrual GL entry
		# (Wage Expense / tax & withholding liabilities / Payroll Payable) via
		# Payroll Entry.make_accrual_jv_entry(), called internally here.
		payroll_entry.submit_salary_slips()

		slip = frappe.get_doc("Salary Slip", slip_name)
		if slip.docstatus != 1:
			frappe.throw(
				_("Salary Slip {0} could not be submitted. {1}").format(
					slip.name, payroll_entry.error_message or ""
				)
			)

		# Step 4 — cash disbursement. make_bank_entry() returns a draft Journal
		# Entry (Bank Entry/Cash Entry) debiting Payroll Payable and crediting
		# the payment account for this slip's net pay; submit it to finish.
		bank_entry = payroll_entry.make_bank_entry()
		if not bank_entry:
			frappe.throw(_("No net-payable amount to disburse for Salary Slip {0}.").format(slip.name))

		# Journal Entry requires a Reference No + Reference Date for voucher_type
		# "Bank Entry" before it can submit (see validate_cheque_info() in Frappe's
		# Journal Entry controller) — normally filled in by hand in Desk. Falls back
		# to something that ties this entry back to the payroll run it came from
		# when the admin doesn't type an actual cheque/wire reference.
		bank_entry.cheque_no = reference_no.strip() if reference_no and reference_no.strip() else payroll_entry.name
		bank_entry.cheque_date = nowdate()
		# custom_remark=1 stops create_remarks() (called from validate(), so it
		# would otherwise run again on submit) from overwriting whatever the
		# admin typed with its own auto-generated "Reference #... dated ..."
		# summary — same pattern as Express Sales/Purchase's Payment Entry
		# custom_remarks flag.
		if remarks and remarks.strip():
			bank_entry.remark = remarks.strip()
			bank_entry.custom_remark = 1
		bank_entry.submit()
		frappe.db.commit()
	except frappe.DoesNotExistError:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Payroll failed")
		# This specific exception is almost always the masking pattern
		# documented in _resolve_payroll_period()'s own comment: some real
		# failure happened deep inside HRMS's create_salary_slips_for_employees()/
		# submit_salary_slips_for_employees(), which caught it internally,
		# logged the real message to a genuine Error Log entry, rolled back
		# (undoing this run's own just-created Payroll Entry along with it),
		# and then a later self.reload() on that now-gone row raises this
		# unrelated-looking "Payroll Entry ... not found" instead. Recover
		# the real message from that Error Log (it's the one artifact that
		# survives the rollback) and show that instead of the opaque default.
		real_failure = (
			_extract_real_payroll_failure(payroll_entry.name)
			if payroll_entry and payroll_entry.name
			else None
		)
		if real_failure:
			message = _(
				"Payroll failed for {0}. The real reason (recovered from HRMS's own internal log,"
				" not the generic error below) was: {1}"
			).format(employee_doc.employee_name, real_failure["message"])
			if real_failure["log_name"]:
				# Plain text, not a link — every error message shown by this page
				# is HTML-escaped client-side before display (see
				# extract_error_message() in payroll.html, added for exactly this
				# reason: an embedded <a> here would just render as literal,
				# escaped markup instead of a real link). Still enough for an
				# admin to open it directly: /app/error-log/<name>.
				message += " " + _("Full details: Error Log {0}.").format(real_failure["log_name"])
			frappe.throw(message)
		raise
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Payroll failed")
		raise

	return {
		"salary_slip": slip.name,
		"payroll_entry": payroll_entry.name,
		"accrual_journal_entry": slip.journal_entry,
		"bank_entry": bank_entry.name,
		"employee": employee_doc.name,
		"employee_name": employee_doc.employee_name,
		"gross_pay": slip.gross_pay,
		"net_pay": slip.rounded_total or slip.net_pay,
		"deductions": deduction_lines or None,
	}


def _get_payroll_bank_entry(payroll_entry: str) -> str | None:
	"""The cash-disbursement Journal Entry (voucher_type Bank Entry/Cash Entry)
	that Payroll Entry.make_bank_entry() creates — same query as HRMS's own
	Payroll Entry.has_bank_entries(), reused here (rather than trusting
	Salary Slip.journal_entry, which only ever holds the *accrual* entry) so
	cancel_express_payroll() below can find and cancel it too.
	"""
	je = frappe.qb.DocType("Journal Entry")
	jea = frappe.qb.DocType("Journal Entry Account")
	rows = (
		frappe.qb.from_(je)
		.inner_join(jea)
		.on(je.name == jea.parent)
		.select(je.name)
		.where(
			((je.voucher_type == "Bank Entry") | (je.voucher_type == "Cash Entry"))
			& (jea.reference_type == "Payroll Entry")
			& (jea.reference_name == payroll_entry)
			& (je.docstatus == 1)
		)
	).run(as_dict=True)
	return rows[0].name if rows else None


@frappe.whitelist()
def cancel_express_payroll(salary_slip: str):
	"""Cancel a payroll run made through this page in one step: the Salary
	Slip, its accrual Journal Entry, its cash-disbursement Journal Entry, and
	the Additional Salary override that set its base pay. Mirrors
	purchase.py's cancel_express_purchase()/sales.py's cancel_express_sale().

	Cancels the Salary Slip directly rather than going through its parent
	Payroll Entry's own cancel (Payroll Entry.on_cancel() ->
	delete_linked_salary_slips() cancels *and then deletes* the Salary Slip)
	— this page's history table is meant to keep showing a cancelled run for
	the record (see get_salary_slip_history()'s docstring), the same way
	Express Purchase/Sales's Cancel leaves a visible "Cancelled" row instead
	of removing it. The Salary Slip is cancelled *before* its Journal
	Entries, matching HRMS's own cascade order in
	Payroll Entry.on_cancel()/delete_linked_salary_slips() — among other
	things, this keeps hrms.payroll.doctype.salary_slip.salary_slip.
	unlink_ref_doc_from_salary_slip() (a Journal Entry on_cancel hook) from
	clearing Salary Slip.journal_entry, since that hook only touches slips
	with docstatus < 2.

	The Additional Salary override is cancelled too so a later run for the
	same employee/period doesn't dead-end on the "already has a base-pay
	override" guard in process_express_payroll() above.
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	slip = frappe.get_doc("Salary Slip", salary_slip)
	if slip.docstatus != 1:
		frappe.throw(_("{0} is not a submitted Salary Slip.").format(salary_slip))

	company = get_selected_company()
	if company and slip.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(salary_slip, company))

	bank_entry_name = _get_payroll_bank_entry(slip.payroll_entry) if slip.payroll_entry else None
	accrual_je_name = slip.journal_entry

	try:
		slip.cancel()

		if bank_entry_name:
			bank_entry = frappe.get_doc("Journal Entry", bank_entry_name)
			if bank_entry.docstatus == 1:
				bank_entry.cancel()

		if accrual_je_name:
			accrual_je = frappe.get_doc("Journal Entry", accrual_je_name)
			if accrual_je.docstatus == 1:
				accrual_je.cancel()

		additional_salary = frappe.db.get_value(
			"Additional Salary",
			{
				"employee": slip.employee,
				"salary_component": BASE_PAY_COMPONENT,
				"overwrite_salary_structure_amount": 1,
				"docstatus": 1,
				"payroll_date": slip.end_date,
			},
		)
		if additional_salary:
			frappe.get_doc("Additional Salary", additional_salary).cancel()

		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Payroll cancel failed")
		raise

	return {
		"salary_slip": slip.name,
		"bank_entry": bank_entry_name,
		"accrual_journal_entry": accrual_je_name,
	}

# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.utils import add_days, flt, format_date, get_first_day, getdate, now_datetime

from ez_accounting.api import ensure_admin, get_selected_company

no_cache = 1


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "reconciliation"
	context.title = _("Bank Reconciliation")
	context.company = get_selected_company()
	return context


def _ensure_account_belongs_to_company(account: str, company: str | None):
	account_doc = frappe.db.get_value("Account", account, ["company"], as_dict=True)
	if not account_doc or (company and account_doc.company != company):
		frappe.throw(_("{0} does not belong to {1}.").format(account, company))


@frappe.whitelist()
def get_bank_accounts():
	ensure_admin()
	company = get_selected_company()
	if not company:
		return []
	return frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Bank", "is_group": 0},
		fields=["name"],
		order_by="name asc",
	)


@frappe.whitelist()
def get_book_balance(account: str, as_of_date: str):
	"""The account's actual running GL balance as of this date — regardless
	of which transactions have cleared the bank yet. This is what
	reconciliation checks against; the admin can't override it, only see
	whether the cleared subset of it matches their bank statement.
	"""
	ensure_admin()

	if not account or not as_of_date:
		frappe.throw(_("Account and date are required."))
	_ensure_account_belongs_to_company(account, get_selected_company())

	balance = frappe.db.sql(
		"""
		SELECT SUM(debit) - SUM(credit) FROM `tabGL Entry`
		WHERE account = %(account)s AND posting_date <= %(as_of_date)s AND is_cancelled = 0
		""",
		{"account": account, "as_of_date": as_of_date},
	)[0][0]
	return {"book_balance": flt(balance)}


def _balance_on(account, date):
	return flt(
		frappe.db.sql(
			"""
			SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0)
			FROM `tabGL Entry`
			WHERE account = %(account)s AND posting_date <= %(date)s AND is_cancelled = 0
			""",
			{"account": account, "date": date},
		)[0][0]
	)


def build_reconciliation_report(account: str, as_of_date: str, statement_balance=0):
	"""Build one shared dataset for the screen's print and Excel actions."""
	ensure_admin()
	company = get_selected_company()
	if not company or not account or not as_of_date:
		frappe.throw(_("Company, bank account, and statement date are required."))
	_ensure_account_belongs_to_company(account, company)

	as_of = getdate(as_of_date)
	period_start = get_first_day(as_of)
	transactions = get_transactions(account, str(as_of), str(period_start), 1)
	all_transactions = get_transactions(account, str(as_of), "", 1)
	beginning = _balance_on(account, add_days(period_start, -1))
	ending = _balance_on(account, as_of)
	receipts = sum(flt(row.amount) for row in transactions if flt(row.amount) > 0)
	disbursements = sum(abs(flt(row.amount)) for row in transactions if flt(row.amount) < 0)
	other = ending - beginning - receipts + disbursements

	# Transit items can predate this statement month, so do not apply the
	# summary period boundary when finding entries still uncleared as of date.
	uncleared = [
		row for row in all_transactions
		if not row.clearance_date or getdate(row.clearance_date) > as_of
	]
	deposits = [row for row in uncleared if flt(row.amount) > 0]
	checks = [row for row in uncleared if flt(row.amount) < 0]
	for row in deposits:
		row["display_amount"] = flt(row.amount)
	for row in checks:
		row["display_amount"] = abs(flt(row.amount))
	total_deposits = sum(row["display_amount"] for row in deposits)
	total_checks = sum(row["display_amount"] for row in checks)
	bank_balance = flt(statement_balance)
	adjusted_bank_balance = bank_balance + total_deposits - total_checks
	difference = ending - adjusted_bank_balance

	return {
		"company_name": company,
		"report_title": _("Account Reconciliation"),
		"as_of_date": format_date(as_of),
		"bank_statement_date": format_date(as_of),
		"account_name": account,
		"filter_criteria": _("Account: {0}; period {1} through {2}").format(
			account, format_date(period_start), format_date(as_of)
		),
		"beginning_gl_balance": beginning,
		"cash_receipts": receipts,
		"cash_disbursements": disbursements,
		"summary_other": other,
		"ending_gl_balance": ending,
		"ending_bank_balance": bank_balance,
		"deposits_in_transit": deposits,
		"total_deposits": total_deposits,
		"outstanding_checks": checks,
		"total_checks": total_checks,
		"detail_other": [],
		"total_detail_other": 0,
		"unreconciled_difference": difference,
		"final_ending_gl_balance": adjusted_bank_balance + difference,
		"period_start": period_start,
		"as_of": as_of,
		"print_date": format_date(now_datetime().date()),
		"print_time": now_datetime().strftime("%I:%M %p"),
		"page_number": 1,
	}


@frappe.whitelist()
def download_excel(account: str, as_of_date: str, statement_balance=0):
	from frappe.utils.xlsxutils import build_xlsx_response

	report = build_reconciliation_report(account, as_of_date, statement_balance)
	out = [
		[report["company_name"]],
		[report["report_title"]],
		[_('As of'), report["as_of_date"]],
		[_('Account'), report["account_name"]],
		[],
		[_('Description'), _('Amount')],
		[_('Beginning GL Balance'), report["beginning_gl_balance"]],
		[_('Add: Cash Receipts'), report["cash_receipts"]],
		[_('Less: Cash Disbursements'), report["cash_disbursements"]],
		[_('Add (Less) Other'), report["summary_other"]],
		[_('Ending GL Balance'), report["ending_gl_balance"]],
		[_('Ending Bank Balance'), report["ending_bank_balance"]],
	]
	for title, rows, total_label, total in (
		(_('Add back deposits in transit'), report["deposits_in_transit"], _('Total deposits in transit'), report["total_deposits"]),
		(_('Less: outstanding checks'), report["outstanding_checks"], _('Total outstanding checks'), report["total_checks"]),
	):
		out.extend([[], [title], [_('Date'), _('Reference Number'), _('Document'), _('Amount')]])
		out.extend([[row.posting_date, row.reference_no or row.name, row.name, row.display_amount] for row in rows])
		out.append([total_label, "", "", total])
	out.extend([[], [_('Unreconciled difference'), report["unreconciled_difference"]], [_('Ending GL Balance'), report["final_ending_gl_balance"]]])
	build_xlsx_response(out, f"Account Reconciliation {report['company_name']} {as_of_date}")


@frappe.whitelist()
def get_transactions(account: str, as_of_date: str, from_date: str = "", include_cleared: int = 1):
	"""Payment Entries AND Journal Entries touching this account, up to
	as_of_date — signed so a positive amount always means money coming INTO
	the account and negative means money going OUT, regardless of which leg
	of the transaction this account was on (same convention ERPNext's own
	Bank Clearance tool uses). Each row carries `voucher_type` ("Payment
	Entry" or "Journal Entry") so the frontend and save_clearance() know which
	doctype to link to / update.

	Journal Entry support added 2026-08-24: this used to be Payment-Entry-only
	on the assumption every real transaction here ran through Express
	Sales/Purchase — no longer true once Forms Bank Deposit Sheet started
	posting its own deposit-to-bank Journal Entries (see
	ez_accounting.ez_accounting.doctype.forms_bank_deposit_sheet). Those were invisible here
	before: they hit the account's book balance (get_book_balance, GL Entry
	based) but never appeared as a line item to check off.
	"""
	ensure_admin()

	if not account or not as_of_date:
		frappe.throw(_("Account and date are required."))
	_ensure_account_belongs_to_company(account, get_selected_company())

	pe_conditions = [
		"(pe.paid_from = %(account)s OR pe.paid_to = %(account)s)",
		"pe.docstatus = 1",
		"pe.posting_date <= %(as_of_date)s",
	]
	values = {"account": account, "as_of_date": as_of_date}
	if from_date:
		pe_conditions.append("pe.posting_date >= %(from_date)s")
		values["from_date"] = from_date
	if not int(include_cleared):
		pe_conditions.append("pe.clearance_date IS NULL")

	pe_rows = frappe.db.sql(
		f"""
		SELECT pe.name, pe.posting_date, pe.payment_type, pe.party, pe.party_type,
			pe.reference_no, pe.paid_from, pe.paid_to, pe.paid_amount, pe.received_amount,
			pe.clearance_date
		FROM `tabPayment Entry` pe
		WHERE {" AND ".join(pe_conditions)}
		ORDER BY pe.posting_date ASC, pe.name ASC
		""",
		values,
		as_dict=True,
	)
	for r in pe_rows:
		r["voucher_type"] = "Payment Entry"
		r["amount"] = flt(r.received_amount) if r.paid_to == account else -flt(r.paid_amount)
		r.pop("received_amount", None)
		r.pop("paid_amount", None)
		r.pop("paid_from", None)
		r.pop("paid_to", None)

	je_conditions = [
		"jea.account = %(account)s",
		"je.docstatus = 1",
		"je.posting_date <= %(as_of_date)s",
	]
	je_values = {"account": account, "as_of_date": as_of_date}
	if from_date:
		je_conditions.append("je.posting_date >= %(from_date)s")
		je_values["from_date"] = from_date
	if not int(include_cleared):
		je_conditions.append("je.clearance_date IS NULL")

	je_rows = frappe.db.sql(
		f"""
		SELECT je.name, je.posting_date, je.voucher_type AS je_voucher_type, je.cheque_no,
			je.clearance_date, SUM(jea.debit_in_account_currency) AS debit,
			SUM(jea.credit_in_account_currency) AS credit
		FROM `tabJournal Entry` je
		INNER JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
		WHERE {" AND ".join(je_conditions)}
		GROUP BY je.name
		ORDER BY je.posting_date ASC, je.name ASC
		""",
		je_values,
		as_dict=True,
	)
	for r in je_rows:
		r["voucher_type"] = "Journal Entry"
		r["payment_type"] = r.pop("je_voucher_type")
		r["party"] = None
		r["party_type"] = None
		r["reference_no"] = r.pop("cheque_no", None)
		r["amount"] = flt(r.pop("debit")) - flt(r.pop("credit"))

	return sorted(pe_rows + je_rows, key=lambda r: (r["posting_date"], r["name"]))


@frappe.whitelist()
def save_clearance(account: str, entries: str | list):
	"""Batch-set clearance_date on Payment Entries and/or Journal Entries.
	`entries` is a JSON list of {name, voucher_type, clearance_date} —
	clearance_date null/blank un-clears that entry. Uses db_set (same as
	ERPNext's own Bank Clearance tool) rather than a full doc.save(), since
	clearance_date is purely a tracking field that doesn't need to re-run
	submit-time validation.
	"""
	ensure_admin()

	if isinstance(entries, str):
		entries = json.loads(entries)

	company = get_selected_company()
	_ensure_account_belongs_to_company(account, company)

	updated = 0
	for entry in entries:
		name = entry.get("name")
		voucher_type = entry.get("voucher_type")
		clearance_date = entry.get("clearance_date") or None

		if voucher_type == "Payment Entry":
			pe_row = frappe.db.get_value(
				"Payment Entry", name, ["paid_from", "paid_to", "docstatus"], as_dict=True
			)
			if not pe_row or pe_row.docstatus != 1:
				continue
			if pe_row.paid_from != account and pe_row.paid_to != account:
				frappe.throw(_("{0} does not touch account {1}.").format(name, account))
			doc = frappe.get_doc("Payment Entry", name)
		elif voucher_type == "Journal Entry":
			if frappe.db.get_value("Journal Entry", name, "docstatus") != 1:
				continue
			if not frappe.db.exists("Journal Entry Account", {"parent": name, "account": account}):
				frappe.throw(_("{0} does not touch account {1}.").format(name, account))
			doc = frappe.get_doc("Journal Entry", name)
		else:
			continue

		doc.db_set("clearance_date", clearance_date)
		updated += 1

	frappe.db.commit()
	return {"updated": updated}

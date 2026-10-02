# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import re
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import flt, getdate

from ez_accounting.api import (
	ensure_logged_in,
	get_date_range_with_default,
	get_quick_ranges,
	get_selected_company,
)

no_cache = 1


def _reference_sort_key(value: str | None) -> tuple:
	"""Sort reference series naturally: CHK-9 comes before CHK-10."""
	value = (value or "").strip()
	if not value:
		return (1, ())
	parts = tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value))
	return (0, parts)


def _account_number(account_name: str, stored_number: str | None) -> str:
	"""The "Account ID" column — same rule as the Cash Receipts Journal
	(see cash_receipts.py)."""
	if stored_number:
		return stored_number
	head = (account_name or "").split(" - ", 1)[0].strip()
	if head and head.replace("-", "").isdigit():
		return head
	return ""


def _first_line(text: str | None) -> str:
	line = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
	return (line[:90] + "…") if len(line) > 90 else line


def _get_check_numbers(company: str) -> list[str]:
	"""Submitted outgoing-payment references available to the Check No. filter."""
	if not company:
		return []
	cash_accounts = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": ["in", ["Cash", "Bank"]], "is_group": 0},
		pluck="name",
	)
	if not cash_accounts:
		return []
	vouchers = frappe.get_all(
		"GL Entry",
		filters={
			"company": company,
			"is_cancelled": 0,
			"is_opening": ["!=", "Yes"],
			"credit": [">", 0],
			"account": ["in", cash_accounts],
		},
		fields=["voucher_type", "voucher_no"],
		distinct=True,
	)
	payment_entries = sorted({row.voucher_no for row in vouchers if row.voucher_type == "Payment Entry"})
	journal_entries = sorted({row.voucher_no for row in vouchers if row.voucher_type == "Journal Entry"})
	values = frappe.get_all(
		"Payment Entry",
		filters={"name": ["in", payment_entries], "reference_no": ["!=", ""]},
		pluck="reference_no",
		distinct=True,
	) if payment_entries else []
	values += frappe.get_all(
		"Journal Entry",
		filters={"name": ["in", journal_entries], "cheque_no": ["!=", ""]},
		pluck="cheque_no",
		distinct=True,
	) if journal_entries else []
	return sorted({str(value).strip() for value in values if str(value or "").strip()}, key=str.casefold)


def _build_cash_disbursements(company: str, from_date: str, to_date: str, check_no: str = "") -> dict:
	"""Every transaction that took money OUT of a Cash/Bank account for
	`company` in the period — one block per voucher, its full set of GL legs
	shown, the way a Peachtree/Sage "Cash Disbursements Journal" prints. The
	credit-side mirror of _build_cash_receipts(): Payment Entries paying
	suppliers, and any Journal Entry that credits a cash account.
	"""
	from_date, to_date = getdate(from_date), getdate(to_date)
	check_no = (check_no or "").strip()

	cash_accounts = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": ["in", ["Cash", "Bank"]], "is_group": 0},
		pluck="name",
	)
	if not cash_accounts:
		return {"vouchers": [], "total_debit": 0.0, "total_credit": 0.0}

	pay_leg_filters = [
		["company", "=", company],
		["is_cancelled", "=", 0],
		["is_opening", "!=", "Yes"],
		["credit", ">", 0],
		["account", "in", cash_accounts],
	]
	if check_no:
		payment_entries = frappe.get_all(
			"Payment Entry",
			filters={"company": company, "payment_type": "Pay", "docstatus": 1, "reference_no": check_no},
			pluck="name",
		)
		journal_entries = frappe.get_all(
			"Journal Entry",
			filters={"company": company, "docstatus": 1, "cheque_no": check_no},
			pluck="name",
		)
		voucher_filter = sorted(set(payment_entries + journal_entries))
		if not voucher_filter:
			return {"vouchers": [], "total_debit": 0.0, "total_credit": 0.0}
		pay_leg_filters.append(["voucher_no", "in", voucher_filter])
	else:
		pay_leg_filters.append(["posting_date", "between", [from_date, to_date]])

	pay_legs = frappe.get_all(
		"GL Entry",
		filters=pay_leg_filters,
		fields=["voucher_no"],
		distinct=True,
	)
	voucher_nos = sorted({r.voucher_no for r in pay_legs})
	if not voucher_nos:
		return {"vouchers": [], "total_debit": 0.0, "total_credit": 0.0}

	legs = frappe.get_all(
		"GL Entry",
		filters=[
			["company", "=", company],
			["voucher_no", "in", voucher_nos],
			["is_cancelled", "=", 0],
		],
		fields=[
			"posting_date",
			"account",
			"party_type",
			"party",
			"against_voucher",
			"debit",
			"credit",
			"voucher_type",
			"voucher_no",
			"remarks",
		],
		order_by="posting_date asc, voucher_no asc, credit desc, account asc",
	)

	accounts = {
		a.name: a
		for a in frappe.get_all(
			"Account",
			filters={"name": ["in", sorted({leg.account for leg in legs})]},
			fields=["name", "account_number", "account_name"],
		)
	}

	pe_names = sorted({leg.voucher_no for leg in legs if leg.voucher_type == "Payment Entry"})
	pe_ref = {}
	if pe_names:
		pe_ref = {
			r.name: r.reference_no
			for r in frappe.get_all(
				"Payment Entry", filters={"name": ["in", pe_names]}, fields=["name", "reference_no"]
			)
		}
	je_names = sorted({leg.voucher_no for leg in legs if leg.voucher_type == "Journal Entry"})
	je_ref = {}
	if je_names:
		je_ref = {
			r.name: r.cheque_no
			for r in frappe.get_all(
				"Journal Entry", filters={"name": ["in", je_names]}, fields=["name", "cheque_no"]
			)
		}

	party_name = {}
	for pt in ("Supplier", "Customer", "Employee"):
		names = sorted({leg.party for leg in legs if leg.party_type == pt and leg.party})
		if not names:
			continue
		label_field = {"Supplier": "supplier_name", "Customer": "customer_name", "Employee": "employee_name"}[pt]
		party_name.update(
			{
				r.name: r.get(label_field) or r.name
				for r in frappe.get_all(pt, filters={"name": ["in", names]}, fields=["name", label_field])
			}
		)

	vouchers: dict[str, dict] = {}
	for leg in legs:
		block = vouchers.setdefault(
			leg.voucher_no,
			{
				"date": leg.posting_date,
				"voucher_type": leg.voucher_type,
				"voucher_no": leg.voucher_no,
				"check_no": pe_ref.get(leg.voucher_no) or je_ref.get(leg.voucher_no) or "",
				"payee": "",
				"lines": [],
			},
		)
		if not block["payee"] and leg.party:
			block["payee"] = party_name.get(leg.party) or leg.party
		acc = accounts.get(leg.account) or frappe._dict()
		if leg.party:
			line_description = party_name.get(leg.party) or leg.party
		else:
			line_description = _first_line(leg.remarks) or acc.account_name or leg.account
		block["lines"].append(
			{
				"account_id": _account_number(leg.account, acc.account_number),
				"account_name": acc.account_name or leg.account,
				"line_description": line_description,
				"debit": flt(leg.debit),
				"credit": flt(leg.credit),
			}
		)

	# Keep the journal chronological. Within each date, follow the visible
	# check/reference series rather than the internal Payment/Journal Entry ID.
	voucher_list = sorted(
		vouchers.values(),
		key=lambda b: (b["date"], _reference_sort_key(b["check_no"]), b["voucher_no"]),
	)
	total_debit = sum(ln["debit"] for b in voucher_list for ln in b["lines"])
	total_credit = sum(ln["credit"] for b in voucher_list for ln in b["lines"])
	return {"vouchers": voucher_list, "total_debit": total_debit, "total_credit": total_credit}


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "cash_disbursements"
	context.title = _("Cash Disbursements Journal")

	company = get_selected_company()
	context.company = company

	from_date, to_date = get_date_range_with_default()
	context.from_date = from_date
	context.to_date = to_date
	context.check_no = (frappe.form_dict.get("check_no") or "").strip()
	context.check_no_encoded = quote(context.check_no, safe="")
	context.check_numbers = _get_check_numbers(company) if company else []

	quick = get_quick_ranges(from_date, to_date)
	context.this_month_from, context.this_month_to = quick["this_month"]
	context.last_month_from, context.last_month_to = quick["last_month"]
	context.quick_active = quick["active"]

	data = (
		_build_cash_disbursements(company, from_date, to_date, context.check_no)
		if company
		else {"vouchers": [], "total_debit": 0.0, "total_credit": 0.0}
	)
	context.vouchers = data["vouchers"]
	context.total_debit = data["total_debit"]
	context.total_credit = data["total_credit"]
	return context


@frappe.whitelist()
def download_excel():
	ensure_logged_in()

	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	from_date, to_date = get_date_range_with_default()
	check_no = (frappe.form_dict.get("check_no") or "").strip()
	data = (
		_build_cash_disbursements(company, from_date, to_date, check_no)
		if company
		else {"vouchers": [], "total_debit": 0.0, "total_credit": 0.0}
	)

	header = [
		_("Date"),
		_("Check #"),
		_("Name"),
		_("Account ID"),
		_("Account Description"),
		_("Line Description"),
		_("Debit Amount"),
		_("Credit Amount"),
	]
	out = [header]
	for block in data["vouchers"]:
		for i, line in enumerate(block["lines"]):
			out.append(
				[
					block["date"] if i == 0 else "",
					block["check_no"] if i == 0 else "",
					block["payee"] if i == 0 else "",
					line["account_id"],
					line["account_name"],
					line["line_description"],
					line["debit"] or "",
					line["credit"] or "",
				]
			)
	if data["vouchers"]:
		out.append(["", "", "", "", "", _("Total"), data["total_debit"], data["total_credit"]])

	suffix = f"Check {check_no}" if check_no else f"{from_date} to {to_date}"
	build_xlsx_response(out, f"Cash Disbursements Journal {company} {suffix}")

# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

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


def _account_number(account_name: str, stored_number: str | None) -> str:
	"""The "Account ID" column — ERPNext's own `account_number` when set,
	otherwise the leading code in the account's name ("10000 - Bank … - EIU"
	-> "10000"), otherwise blank."""
	if stored_number:
		return stored_number
	head = (account_name or "").split(" - ", 1)[0].strip()
	# Only treat it as a code if it actually looks like one (digits, maybe a
	# "-000" suffix) — plain-named accounts ("Cash - EIU") shouldn't show "Cash".
	if head and head.replace("-", "").isdigit():
		return head
	return ""


def _build_cash_receipts(company: str, from_date: str, to_date: str) -> dict:
	"""Every transaction that put money into a Cash/Bank account for `company`
	in the period — one block per voucher, its full set of GL legs shown, the
	way a Peachtree/Sage "Cash Receipts Journal" prints. Almost always Payment
	Entries (Express Sales / Record Payment receipts), but any Journal Entry
	that debits a cash account is a cash receipt too and is included.

	Plain frappe.get_all() reads assembled in Python — same "no bespoke SQL
	aggregate" discipline as /forms/user/ar.
	"""
	from_date, to_date = getdate(from_date), getdate(to_date)

	cash_accounts = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": ["in", ["Cash", "Bank"]], "is_group": 0},
		pluck="name",
	)
	if not cash_accounts:
		return {"vouchers": [], "total_debit": 0.0, "total_credit": 0.0}

	receipt_legs = frappe.get_all(
		"GL Entry",
		filters=[
			["company", "=", company],
			["posting_date", "between", [from_date, to_date]],
			["is_cancelled", "=", 0],
			["is_opening", "!=", "Yes"],
			["debit", ">", 0],
			["account", "in", cash_accounts],
		],
		fields=["voucher_no"],
		distinct=True,
	)
	voucher_nos = sorted({r.voucher_no for r in receipt_legs})
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
		order_by="posting_date asc, voucher_no asc, debit desc, account asc",
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
	for pt in ("Customer", "Supplier"):
		names = sorted({leg.party for leg in legs if leg.party_type == pt and leg.party})
		if not names:
			continue
		label_field = "customer_name" if pt == "Customer" else "supplier_name"
		party_name.update(
			{
				r.name: r.get(label_field) or r.name
				for r in frappe.get_all(pt, filters={"name": ["in", names]}, fields=["name", label_field])
			}
		)

	vouchers: dict[tuple, dict] = {}
	for leg in legs:
		key = (leg.voucher_type, leg.voucher_no)
		block = vouchers.setdefault(
			key,
			{
				"date": leg.posting_date,
				"voucher_type": leg.voucher_type,
				"voucher_no": leg.voucher_no,
				"lines": [],
			},
		)
		acc = accounts.get(leg.account) or frappe._dict()
		ref = leg.against_voucher or pe_ref.get(leg.voucher_no) or je_ref.get(leg.voucher_no) or leg.voucher_no
		if leg.party:
			line_description = party_name.get(leg.party) or leg.party
		else:
			line_description = acc.account_name or leg.account
		block["lines"].append(
			{
				"account_id": _account_number(leg.account, acc.account_number),
				"account_name": acc.account_name or leg.account,
				"ref": ref,
				"line_description": line_description,
				"debit": flt(leg.debit),
				"credit": flt(leg.credit),
			}
		)

	voucher_list = sorted(vouchers.values(), key=lambda b: (b["date"], b["voucher_no"]))
	total_debit = sum(ln["debit"] for b in voucher_list for ln in b["lines"])
	total_credit = sum(ln["credit"] for b in voucher_list for ln in b["lines"])
	return {"vouchers": voucher_list, "total_debit": total_debit, "total_credit": total_credit}


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "cash_receipts"
	context.title = _("Cash Receipts Journal")

	company = get_selected_company()
	context.company = company

	from_date, to_date = get_date_range_with_default()
	context.from_date = from_date
	context.to_date = to_date

	quick = get_quick_ranges(from_date, to_date)
	context.this_month_from, context.this_month_to = quick["this_month"]
	context.last_month_from, context.last_month_to = quick["last_month"]
	context.quick_active = quick["active"]

	data = (
		_build_cash_receipts(company, from_date, to_date)
		if company
		else {"vouchers": [], "total_debit": 0.0, "total_credit": 0.0}
	)
	context.vouchers = data["vouchers"]
	context.total_debit = data["total_debit"]
	context.total_credit = data["total_credit"]
	context.line_count = sum(len(v["lines"]) for v in data["vouchers"])
	return context


@frappe.whitelist()
def download_excel():
	ensure_logged_in()

	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	from_date, to_date = get_date_range_with_default()
	data = (
		_build_cash_receipts(company, from_date, to_date)
		if company
		else {"vouchers": [], "total_debit": 0.0, "total_credit": 0.0}
	)

	header = [
		_("Date"),
		_("Account ID"),
		_("Account Description"),
		_("Transaction Ref"),
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
					line["account_id"],
					line["account_name"],
					line["ref"],
					line["line_description"],
					line["debit"] or "",
					line["credit"] or "",
				]
			)
	if data["vouchers"]:
		out.append(["", "", "", "", _("Total"), data["total_debit"], data["total_credit"]])

	build_xlsx_response(out, f"Cash Receipts Journal {company} {from_date} to {to_date}")

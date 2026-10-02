# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate

from ez_accounting.api import (
	apply_permitted_filter,
	ensure_logged_in,
	get_date_range_with_default,
	get_quick_ranges,
	get_selected_company,
)

no_cache = 1


def _account_number(account_name: str, stored_number: str | None) -> str:
	"""The "Account ID" column — same rule as the Sales Journal / Cash Receipts
	Journal (see sales_journal.py)."""
	if stored_number:
		return stored_number
	head = (account_name or "").split(" - ", 1)[0].strip()
	if head and head.replace("-", "").isdigit():
		return head
	return ""


def _build_purchase_journal(company: str, from_date: str, to_date: str) -> dict:
	"""One block per Purchase Invoice (submitted, incl. debit notes) for
	`company` posted in the period, its full set of GL legs shown — the
	buy-side mirror of _build_sales_journal(). Expense lines carry the
	invoice's own item descriptions where they line up 1:1 with the GL
	account; the payable line carries the supplier name.
	"""
	from_date, to_date = getdate(from_date), getdate(to_date)

	inv_filters = [
		["company", "=", company],
		["docstatus", "=", 1],
		["posting_date", "between", [from_date, to_date]],
	]
	inv_filters = apply_permitted_filter(inv_filters, "Supplier", "supplier")
	invoices = frappe.get_all(
		"Purchase Invoice",
		filters=inv_filters,
		fields=["name", "posting_date", "is_return", "bill_no"],
		order_by="posting_date asc, name asc",
	)
	invoice_names = [inv.name for inv in invoices]
	if not invoice_names:
		return {"vouchers": [], "total_debit": 0.0, "total_credit": 0.0}

	legs = frappe.get_all(
		"GL Entry",
		filters=[
			["company", "=", company],
			["voucher_type", "=", "Purchase Invoice"],
			["voucher_no", "in", invoice_names],
			["is_cancelled", "=", 0],
		],
		fields=[
			"posting_date",
			"account",
			"party_type",
			"party",
			"debit",
			"credit",
			"voucher_no",
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

	# Item descriptions per (invoice, expense account) — joined when more than
	# one item on the invoice hits the same account (the GL line is summed).
	desc_by_line: dict[tuple, list[str]] = {}
	for it in frappe.get_all(
		"Purchase Invoice Item",
		filters={"parent": ["in", invoice_names]},
		fields=["parent", "expense_account", "item_name", "description"],
		order_by="parent asc, idx asc",
	):
		text = (it.description or it.item_name or "").strip()
		if not text:
			continue
		bucket = desc_by_line.setdefault((it.parent, it.expense_account), [])
		if text not in bucket:
			bucket.append(text)

	party_names = {}
	names = sorted({leg.party for leg in legs if leg.party_type == "Supplier" and leg.party})
	if names:
		party_names = {
			r.name: r.supplier_name or r.name
			for r in frappe.get_all("Supplier", filters={"name": ["in", names]}, fields=["name", "supplier_name"])
		}

	bill_no = {inv.name: (inv.bill_no or "") for inv in invoices}

	vouchers: dict[str, dict] = {}
	for leg in legs:
		block = vouchers.setdefault(
			leg.voucher_no,
			{
				"date": leg.posting_date,
				"invoice": leg.voucher_no,
				"bill_no": bill_no.get(leg.voucher_no, ""),
				"lines": [],
			},
		)
		acc = accounts.get(leg.account) or frappe._dict()
		if leg.party:
			line_description = party_names.get(leg.party) or leg.party
		else:
			descs = desc_by_line.get((leg.voucher_no, leg.account))
			line_description = ", ".join(descs) if descs else (acc.account_name or leg.account)
		block["lines"].append(
			{
				"account_id": _account_number(leg.account, acc.account_number),
				"account_name": acc.account_name or leg.account,
				"line_description": line_description,
				"debit": flt(leg.debit),
				"credit": flt(leg.credit),
			}
		)

	voucher_list = sorted(vouchers.values(), key=lambda b: (b["date"], b["invoice"]))
	total_debit = sum(ln["debit"] for b in voucher_list for ln in b["lines"])
	total_credit = sum(ln["credit"] for b in voucher_list for ln in b["lines"])
	return {"vouchers": voucher_list, "total_debit": total_debit, "total_credit": total_credit}


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "purchase_journal"
	context.title = _("Purchase Journal")

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
		_build_purchase_journal(company, from_date, to_date)
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
	data = (
		_build_purchase_journal(company, from_date, to_date)
		if company
		else {"vouchers": [], "total_debit": 0.0, "total_credit": 0.0}
	)

	header = [
		_("Date"),
		_("Account ID"),
		_("Account Description"),
		_("Invoice/CM #"),
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
					block["invoice"] if i == 0 else "",
					line["line_description"],
					line["debit"] or "",
					line["credit"] or "",
				]
			)
	if data["vouchers"]:
		out.append(["", "", "", "", _("Total"), data["total_debit"], data["total_credit"]])

	build_xlsx_response(out, f"Purchase Journal {company} {from_date} to {to_date}")

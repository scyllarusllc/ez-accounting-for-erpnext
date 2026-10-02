# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate

from frappe.query_builder.functions import Sum

from ez_accounting.api import (
	apply_permitted_filter,
	ensure_logged_in,
	get_date_range_with_default,
	get_quick_ranges,
	get_selected_company,
	permitted_docs,
)

no_cache = 1

# voucher_type -> the short journal code EIS's old ledger printed in the "Type"
# column. "CRJ" (Cash Receipts Journal) is the one that shows up most on this
# report — see [[forms-cash-receipts-journal]].
TYPE_CODE = {
	"Sales Invoice": "SJ",
	"Payment Entry": "CRJ",
	"Journal Entry": "GENJ",
	"Dunning": "DUN",
}


def _type_code(voucher_type: str) -> str:
	if voucher_type in TYPE_CODE:
		return TYPE_CODE[voucher_type]
	# Fall back to the initials of the voucher type ("Delivery Note" -> "DN").
	return "".join(word[0] for word in (voucher_type or "").split() if word).upper() or "—"


def _build_customer_ledgers(company: str, from_date: str, to_date: str) -> dict:
	"""One ledger block per customer that has a receivable balance carried into
	the period or any receivable activity during it — a "Balance Fwd" opening
	line, then every transaction with a running balance, the way EIS's old
	Peachtree "Customer Ledgers" report printed.

	Receivable activity = GL Entries with `party_type = "Customer"` on any
	Receivable-type account for `company`. Plain frappe.get_all() reads summed
	in Python, same discipline as the other forms report pages.
	"""
	from_date, to_date = getdate(from_date), getdate(to_date)

	receivable_accounts = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Receivable", "is_group": 0},
		pluck="name",
	)
	if not receivable_accounts:
		return {"customers": [], "total_debit": 0.0, "total_credit": 0.0, "total_balance": 0.0}

	base = [
		["company", "=", company],
		["party_type", "=", "Customer"],
		["account", "in", receivable_accounts],
		["is_cancelled", "=", 0],
	]
	base = apply_permitted_filter(base, "Customer", "party")
	permitted_parties = permitted_docs("Customer")

	# Opening ("Balance Fwd") balance per customer — everything before the
	# period. Query-builder (not frappe.get_all) because this Frappe rejects
	# SQL aggregate functions passed as `fields` strings.
	gle = frappe.qb.DocType("GL Entry")
	opening_q = (
		frappe.qb.from_(gle)
		.select(gle.party, Sum(gle.debit).as_("debit"), Sum(gle.credit).as_("credit"))
		.where(
			(gle.company == company)
			& (gle.party_type == "Customer")
			& (gle.is_cancelled == 0)
			& (gle.account.isin(receivable_accounts))
			& (gle.posting_date < from_date)
		)
		.groupby(gle.party)
	)
	if permitted_parties is not None:
		opening_q = opening_q.where(gle.party.isin(permitted_parties))
	opening = {r.party: flt(r.debit) - flt(r.credit) for r in opening_q.run(as_dict=True)}

	txn_rows = frappe.get_all(
		"GL Entry",
		filters=[*base, ["posting_date", "between", [from_date, to_date]]],
		fields=[
			"party",
			"posting_date",
			"voucher_type",
			"voucher_no",
			"debit",
			"credit",
		],
		order_by="posting_date asc, creation asc",
	)
	txns_by_party: dict[str, list] = {}
	for row in txn_rows:
		txns_by_party.setdefault(row.party, []).append(row)

	parties = {p for p, bal in opening.items() if round(bal, 2) != 0} | set(txns_by_party)
	if not parties:
		return {"customers": [], "total_debit": 0.0, "total_credit": 0.0, "total_balance": 0.0}

	names = {
		r.name: r.customer_name or r.name
		for r in frappe.get_all(
			"Customer", filters={"name": ["in", sorted(parties)]}, fields=["name", "customer_name"]
		)
	}

	customers = []
	total_debit = total_credit = total_balance = 0.0
	for party in sorted(parties, key=lambda p: (names.get(p, p) or p).lower()):
		ob = opening.get(party, 0.0)
		balance = ob
		rows = []
		for tx in txns_by_party.get(party, []):
			balance += flt(tx.debit) - flt(tx.credit)
			rows.append(
				{
					"date": tx.posting_date,
					"trans_no": tx.voucher_no,
					"type": _type_code(tx.voucher_type),
					"debit": flt(tx.debit),
					"credit": flt(tx.credit),
					"balance": balance,
				}
			)
			total_debit += flt(tx.debit)
			total_credit += flt(tx.credit)

		customers.append(
			{
				"customer_id": party,
				"customer": names.get(party, party),
				"opening": ob,
				"rows": rows,
				"closing": balance,
				"no_activity": not rows,
			}
		)
		total_balance += balance

	return {
		"customers": customers,
		"total_debit": total_debit,
		"total_credit": total_credit,
		"total_balance": total_balance,
	}


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "customer_ledgers"
	context.title = _("Customer Ledgers")

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
		_build_customer_ledgers(company, from_date, to_date)
		if company
		else {"customers": [], "total_debit": 0.0, "total_credit": 0.0, "total_balance": 0.0}
	)
	context.customers = data["customers"]
	context.total_debit = data["total_debit"]
	context.total_credit = data["total_credit"]
	context.total_balance = data["total_balance"]
	return context


@frappe.whitelist()
def download_excel():
	ensure_logged_in()

	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	from_date, to_date = get_date_range_with_default()
	data = (
		_build_customer_ledgers(company, from_date, to_date)
		if company
		else {"customers": [], "total_debit": 0.0, "total_credit": 0.0, "total_balance": 0.0}
	)

	header = [
		_("Customer ID"),
		_("Customer"),
		_("Date"),
		_("Trans No"),
		_("Type"),
		_("Debit Amt"),
		_("Credit Amt"),
		_("Balance"),
	]
	out = [header]
	for c in data["customers"]:
		out.append([c["customer_id"], c["customer"], "", "", _("Balance Fwd"), "", "", c["opening"]])
		if c["no_activity"]:
			out.append(["", "", "", "", _("No Activity"), "", "", ""])
		for row in c["rows"]:
			out.append(
				["", "", row["date"], row["trans_no"], row["type"], row["debit"] or "", row["credit"] or "", row["balance"]]
			)
	if data["customers"]:
		out.append(
			["", _("Report Total"), "", "", "", data["total_debit"], data["total_credit"], data["total_balance"]]
		)

	build_xlsx_response(out, f"Customer Ledgers {company} {from_date} to {to_date}")

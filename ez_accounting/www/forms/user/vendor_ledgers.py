# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import flt, getdate

from ez_accounting.api import (
	apply_permitted_filter,
	ensure_logged_in,
	get_date_range_with_default,
	get_quick_ranges,
	get_selected_company,
	permitted_docs,
)

no_cache = 1

# voucher_type -> the short journal code EIS's old ledger printed in the "Type
# Paid" column. "CDJ" (Cash Disbursements Journal) is the one that shows up
# most here — see [[forms-cash-disbursements-journal]]. The payables mirror of
# customer_ledgers.TYPE_CODE.
TYPE_CODE = {
	"Purchase Invoice": "PJ",
	"Payment Entry": "CDJ",
	"Journal Entry": "GENJ",
}


def _type_code(voucher_type: str) -> str:
	if voucher_type in TYPE_CODE:
		return TYPE_CODE[voucher_type]
	return "".join(word[0] for word in (voucher_type or "").split() if word).upper() or "—"


def _build_vendor_ledgers(company: str, from_date: str, to_date: str) -> dict:
	"""One ledger block per supplier that has a payable balance carried into
	the period or any payable activity during it — a "Balance Fwd" opening
	line, then every transaction with a running balance, the way EIS's old
	Peachtree "Vendor Ledgers" report printed. The payables mirror of
	_build_customer_ledgers().

	Payable activity = GL Entries with `party_type = "Supplier"` on any
	Payable-type account for `company`. Balance is credit-minus-debit (a
	positive balance = money owed to the vendor).
	"""
	from_date, to_date = getdate(from_date), getdate(to_date)

	payable_accounts = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Payable", "is_group": 0},
		pluck="name",
	)
	if not payable_accounts:
		return {"vendors": [], "total_debit": 0.0, "total_credit": 0.0, "total_balance": 0.0}

	base = [
		["company", "=", company],
		["party_type", "=", "Supplier"],
		["account", "in", payable_accounts],
		["is_cancelled", "=", 0],
	]
	base = apply_permitted_filter(base, "Supplier", "party")
	permitted_parties = permitted_docs("Supplier")

	# Opening ("Balance Fwd") balance per supplier — everything before the
	# period. Query-builder (not frappe.get_all) because this Frappe rejects
	# SQL aggregate functions passed as `fields` strings.
	gle = frappe.qb.DocType("GL Entry")
	opening_q = (
		frappe.qb.from_(gle)
		.select(gle.party, Sum(gle.debit).as_("debit"), Sum(gle.credit).as_("credit"))
		.where(
			(gle.company == company)
			& (gle.party_type == "Supplier")
			& (gle.is_cancelled == 0)
			& (gle.account.isin(payable_accounts))
			& (gle.posting_date < from_date)
		)
		.groupby(gle.party)
	)
	if permitted_parties is not None:
		opening_q = opening_q.where(gle.party.isin(permitted_parties))
	opening = {r.party: flt(r.credit) - flt(r.debit) for r in opening_q.run(as_dict=True)}

	txn_rows = frappe.get_all(
		"GL Entry",
		filters=[*base, ["posting_date", "between", [from_date, to_date]]],
		fields=["party", "posting_date", "voucher_type", "voucher_no", "debit", "credit"],
		order_by="posting_date asc, creation asc",
	)
	txns_by_party: dict[str, list] = {}
	for row in txn_rows:
		txns_by_party.setdefault(row.party, []).append(row)

	parties = {p for p, bal in opening.items() if round(bal, 2) != 0} | set(txns_by_party)
	if not parties:
		return {"vendors": [], "total_debit": 0.0, "total_credit": 0.0, "total_balance": 0.0}

	names = {
		r.name: r.supplier_name or r.name
		for r in frappe.get_all(
			"Supplier", filters={"name": ["in", sorted(parties)]}, fields=["name", "supplier_name"]
		)
	}

	vendors = []
	total_debit = total_credit = total_balance = 0.0
	for party in sorted(parties, key=lambda p: (names.get(p, p) or p).lower()):
		ob = opening.get(party, 0.0)
		balance = ob
		rows = []
		for tx in txns_by_party.get(party, []):
			balance += flt(tx.credit) - flt(tx.debit)
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

		vendors.append(
			{
				"vendor_id": party,
				"vendor": names.get(party, party),
				"opening": ob,
				"rows": rows,
				"closing": balance,
				"no_activity": not rows,
			}
		)
		total_balance += balance

	return {
		"vendors": vendors,
		"total_debit": total_debit,
		"total_credit": total_credit,
		"total_balance": total_balance,
	}


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "vendor_ledgers"
	context.title = _("Vendor Ledgers")

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
		_build_vendor_ledgers(company, from_date, to_date)
		if company
		else {"vendors": [], "total_debit": 0.0, "total_credit": 0.0, "total_balance": 0.0}
	)
	context.vendors = data["vendors"]
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
		_build_vendor_ledgers(company, from_date, to_date)
		if company
		else {"vendors": [], "total_debit": 0.0, "total_credit": 0.0, "total_balance": 0.0}
	)

	header = [
		_("Vendor ID"),
		_("Vendor"),
		_("Date"),
		_("Trans No"),
		_("Type Paid"),
		_("Debit Amt"),
		_("Credit Amt"),
		_("Balance"),
	]
	out = [header]
	for v in data["vendors"]:
		out.append([v["vendor_id"], v["vendor"], "", "", _("Balance Fwd"), "", "", v["opening"]])
		if v["no_activity"]:
			out.append(["", "", "", "", _("No Activity"), "", "", ""])
		for row in v["rows"]:
			out.append(
				["", "", row["date"], row["trans_no"], row["type"], row["debit"] or "", row["credit"] or "", row["balance"]]
			)
	if data["vendors"]:
		out.append(
			["", _("Report Total"), "", "", "", data["total_debit"], data["total_credit"], data["total_balance"]]
		)

	build_xlsx_response(out, f"Vendor Ledgers {company} {from_date} to {to_date}")

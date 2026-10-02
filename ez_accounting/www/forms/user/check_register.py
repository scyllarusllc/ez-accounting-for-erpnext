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


def _build_check_register(company: str, from_date: str, to_date: str) -> dict:
	"""One row per check written from a Cash/Bank account for `company` in the
	period — Payment Entries (payment_type "Pay") and check-type Journal
	Entries (a cheque number, crediting a cash account and debiting something
	that isn't one — i.e. a real payment out, not an internal bank transfer
	like a deposit-sheet JE). Cancelled ones (docstatus 2) show as VOID and
	are excluded from the Total.
	"""
	from_date, to_date = getdate(from_date), getdate(to_date)

	cash_accounts = set(
		frappe.get_all(
			"Account",
			filters={"company": company, "account_type": ["in", ["Cash", "Bank"]], "is_group": 0},
			pluck="name",
		)
	)
	if not cash_accounts:
		return {"checks": [], "total": 0.0, "void_count": 0}

	account_name = {
		a.name: a.account_name
		for a in frappe.get_all("Account", filters={"name": ["in", list(cash_accounts)]}, fields=["name", "account_name"])
	}

	rows = []

	# --- Payment Entries ---
	pes = frappe.get_all(
		"Payment Entry",
		filters=[
			["company", "=", company],
			["payment_type", "=", "Pay"],
			["docstatus", "in", [1, 2]],
			["posting_date", "between", [from_date, to_date]],
			["paid_from", "in", list(cash_accounts)],
		],
		fields=[
			"name",
			"reference_no",
			"reference_date",
			"posting_date",
			"party_name",
			"party",
			"paid_from",
			"paid_amount",
			"docstatus",
		],
	)
	for pe in pes:
		rows.append(
			{
				"check_no": (pe.reference_no or "").strip() or pe.name,
				"date": pe.reference_date or pe.posting_date,
				"payee": pe.party_name or pe.party or "",
				"cash_account": account_name.get(pe.paid_from) or pe.paid_from,
				"amount": flt(pe.paid_amount),
				"void": pe.docstatus == 2,
				"voucher": pe.name,
			}
		)

	# --- check-type Journal Entries ---
	jes = frappe.get_all(
		"Journal Entry",
		filters=[
			["company", "=", company],
			["voucher_type", "in", ["Bank Entry", "Cash Entry"]],
			["docstatus", "in", [1, 2]],
			["cheque_no", "!=", ""],
			["posting_date", "between", [from_date, to_date]],
		],
		fields=["name", "cheque_no", "cheque_date", "posting_date", "pay_to_recd_from", "user_remark", "docstatus"],
	)
	je_names = [j.name for j in jes]
	jea_by_parent: dict[str, list] = {}
	if je_names:
		jea = frappe.get_all(
			"Journal Entry Account",
			filters={"parent": ["in", je_names]},
			fields=["parent", "account", "party_type", "party", "debit", "credit"],
		)
		leaf_types = {
			a.name: a.account_type
			for a in frappe.get_all("Account", filters={"name": ["in", list({r.account for r in jea})]}, fields=["name", "account_type"])
		}
		for r in jea:
			r["account_type"] = leaf_types.get(r.account)
			jea_by_parent.setdefault(r.parent, []).append(r)

	supplier_names = {}
	for j in jes:
		for a in jea_by_parent.get(j.name, []):
			if a.party_type == "Supplier" and a.party:
				supplier_names[a.party] = None
	if supplier_names:
		supplier_names = {
			r.name: r.supplier_name
			for r in frappe.get_all("Supplier", filters={"name": ["in", list(supplier_names)]}, fields=["name", "supplier_name"])
		}

	for j in jes:
		accs = jea_by_parent.get(j.name, [])
		cash_credit = [a for a in accs if a.account_type in ("Bank", "Cash") and flt(a.credit) > 0]
		other_debit = [a for a in accs if a.account_type not in ("Bank", "Cash") and flt(a.debit) > 0]
		if not (cash_credit and other_debit):
			continue  # internal transfer / not a check to a payee
		cash_leg = cash_credit[0]
		payee = (j.pay_to_recd_from or "").strip()
		if not payee:
			party_leg = next((a for a in other_debit if a.party), None)
			if party_leg:
				payee = supplier_names.get(party_leg.party) or party_leg.party
		if not payee and j.user_remark:
			payee = j.user_remark.strip().splitlines()[0].strip()[:80]
		if not payee:
			payee = frappe.db.get_value("Account", other_debit[0].account, "account_name") or other_debit[0].account
		rows.append(
			{
				"check_no": (j.cheque_no or "").strip() or j.name,
				"date": j.cheque_date or j.posting_date,
				"payee": payee,
				"cash_account": account_name.get(cash_leg.account) or cash_leg.account,
				"amount": sum(flt(a.credit) for a in cash_credit),
				"void": j.docstatus == 2,
				"voucher": j.name,
			}
		)

	rows.sort(key=lambda r: (r["date"], str(r["check_no"])))
	total = sum(r["amount"] for r in rows if not r["void"])
	void_count = sum(1 for r in rows if r["void"])
	return {"checks": rows, "total": total, "void_count": void_count}


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "check_register"
	context.title = _("Check Register")

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
		_build_check_register(company, from_date, to_date)
		if company
		else {"checks": [], "total": 0.0, "void_count": 0}
	)
	context.checks = data["checks"]
	context.report_total = data["total"]
	context.void_count = data["void_count"]
	return context


@frappe.whitelist()
def download_excel():
	ensure_logged_in()

	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	from_date, to_date = get_date_range_with_default()
	data = (
		_build_check_register(company, from_date, to_date)
		if company
		else {"checks": [], "total": 0.0, "void_count": 0}
	)

	header = [_("Check #"), _("Date"), _("Payee"), _("Cash Account"), _("Amount"), _("Status")]
	out = [header]
	for c in data["checks"]:
		out.append(
			[
				c["check_no"],
				c["date"],
				c["payee"],
				c["cash_account"],
				"" if c["void"] else c["amount"],
				_("VOID") if c["void"] else "",
			]
		)
	if data["checks"]:
		out.append(["", "", "", _("Total"), data["total"], ""])

	build_xlsx_response(out, f"Check Register {company} {from_date} to {to_date}")

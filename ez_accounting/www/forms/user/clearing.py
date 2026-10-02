# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from ez_accounting.api import ensure_admin, get_selected_company
from ez_accounting.ez_accounting.doctype.forms_clearing_sweep.forms_clearing_sweep import get_clearing_balance

no_cache = 1


def get_context(context):
	"""Step 3 of the "discount clearing account" workflow: front desk pays off
	tuition discounts / internal grants with a virtual Cash Mode of Payment
	that parks the money in a Cash-type clearing account; here, finance sweeps
	that accumulated fake balance into an offset account with one Journal
	Entry (Debit offset, Credit clearing). The offset account is either a real
	Expense account or an Income account used as contra-revenue (see
	get_offset_accounts()). Structurally the same as the Deposit page (which
	sweeps Undeposited Funds into a Bank account) — see
	ez_accounting.www.forms.user.deposit.
	"""
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "clearing"
	context.title = _("Discount Clearing")
	context.company = get_selected_company()
	return context


def _require_company() -> str:
	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))
	return company


@frappe.whitelist()
def get_clearing_accounts():
	"""Cash-type leaf accounts for the selected company — the clearing
	accounts a virtual Mode of Payment can pay into."""
	ensure_admin()
	company = get_selected_company()
	if not company:
		return []
	return frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Cash", "is_group": 0},
		fields=["name"],
		order_by="name asc",
	)


@frappe.whitelist()
def get_offset_accounts():
	"""Leaf accounts a clearing balance can be swept into: an **Expense**
	account (a real cost), or an **Income** account used as a contra-revenue
	account (e.g. "Tuition Fee - Working Hrs" — debiting it nets down gross
	revenue, the standard way a non-profit records a tuition discount instead
	of booking a fake salary cost). Both are legitimate; the CoA node the
	admin picks decides how the discount shows on the P&L.
	"""
	ensure_admin()
	company = get_selected_company()
	if not company:
		return []
	return frappe.get_all(
		"Account",
		filters={"company": company, "is_group": 0, "root_type": ["in", ["Expense", "Income"]]},
		fields=["name", "root_type"],
		order_by="root_type asc, name asc",
	)


@frappe.whitelist()
def get_clearing_settings():
	"""The remembered clearing-account -> expense-account pairing for this
	company (upserted by process_clearing_sweep), or {} if never set."""
	ensure_admin()
	company = get_selected_company()
	if not company:
		return {}
	if not frappe.db.exists("Forms Clearing Settings", company):
		return {"company": company}
	doc = frappe.db.get_value(
		"Forms Clearing Settings",
		company,
		["default_clearing_account", "default_expense_account"],
		as_dict=True,
	) or {}
	doc["company"] = company
	return doc


def _validate_clearing_account(account: str, company: str):
	account = (account or "").strip()
	if not account:
		return None
	acct = frappe.db.get_value("Account", account, ["company", "is_group", "account_type"], as_dict=True)
	if not acct or acct.is_group or acct.company != company:
		frappe.throw(_("{0} is not a valid account for {1}.").format(account, company))
	if acct.account_type != "Cash":
		frappe.throw(_("Clearing Account must be a Cash-type account. {0} is not.").format(account))
	return account


def _validate_offset_account(account: str, company: str):
	account = (account or "").strip()
	if not account:
		return None
	acct = frappe.db.get_value("Account", account, ["company", "is_group", "root_type"], as_dict=True)
	if not acct or acct.is_group or acct.company != company:
		frappe.throw(_("{0} is not a valid account for {1}.").format(account, company))
	if acct.root_type not in ("Expense", "Income"):
		frappe.throw(
			_("The offset account must be an Expense or Income (contra-revenue) account. {0} is neither.").format(
				account
			)
		)
	return account


def _save_clearing_settings(company: str, clearing_account, expense_account):
	if frappe.db.exists("Forms Clearing Settings", company):
		doc = frappe.get_doc("Forms Clearing Settings", company)
	else:
		doc = frappe.new_doc("Forms Clearing Settings")
		doc.company = company
	doc.default_clearing_account = clearing_account or None
	doc.default_expense_account = expense_account or None
	doc.save(ignore_permissions=True)


@frappe.whitelist()
def save_clearing_settings(clearing_account: str = "", expense_account: str = ""):
	"""Set (or clear, with a blank value) the selected company's default
	Clearing / Expense account pairing — the same Forms Clearing Settings row
	process_clearing_sweep() upserts automatically, but editable on its own so
	finance can configure it up front. Each account, if given, is validated
	the same way the doctype's own validate() does.
	"""
	ensure_admin()
	company = _require_company()

	clearing_account = _validate_clearing_account(clearing_account, company)
	expense_account = _validate_offset_account(expense_account, company)

	_save_clearing_settings(company, clearing_account, expense_account)
	frappe.db.commit()

	return {
		"company": company,
		"default_clearing_account": clearing_account,
		"default_expense_account": expense_account,
	}


@frappe.whitelist()
def get_clearing_balance_preview(clearing_account: str, as_of_date: str = ""):
	"""The clearing account's GL balance (un-swept amount) as of a date."""
	ensure_admin()
	company = _require_company()
	if not clearing_account:
		return {"balance": 0}
	acct_company = frappe.db.get_value("Account", clearing_account, "company")
	if acct_company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(clearing_account, company))
	return {"balance": get_clearing_balance(clearing_account, getdate(as_of_date or nowdate()))}


@frappe.whitelist()
def get_clearing_entries(clearing_account: str, from_date: str = "", to_date: str = ""):
	"""What's sitting in the clearing account: submitted Payment Entries that
	paid INTO it, plus Journal Entries that debited it, in the date range —
	the trail behind the balance, same idea as the deposit page's
	"Payment Entries in Range" list.
	"""
	ensure_admin()
	company = _require_company()
	if not clearing_account:
		return {"entries": [], "summary": {"count": 0, "total": 0}}
	if frappe.db.get_value("Account", clearing_account, "company") != company:
		frappe.throw(_("{0} does not belong to {1}.").format(clearing_account, company))

	values = {"account": clearing_account, "company": company}
	date_cond = ""
	if from_date:
		values["from_date"] = getdate(from_date)
		date_cond += " AND pe.posting_date >= %(from_date)s"
	if to_date:
		values["to_date"] = getdate(to_date)
		date_cond += " AND pe.posting_date <= %(to_date)s"

	pe_rows = frappe.db.sql(
		f"""
		SELECT pe.name, pe.posting_date, pe.party_type, pe.party, pe.party_name,
			pe.mode_of_payment, pe.reference_no, pe.paid_amount AS amount
		FROM `tabPayment Entry` pe
		WHERE pe.docstatus = 1 AND pe.company = %(company)s AND pe.paid_to = %(account)s
			{date_cond}
		ORDER BY pe.posting_date ASC, pe.creation ASC
		""",
		values,
		as_dict=True,
	)
	for r in pe_rows:
		r["voucher_type"] = "Payment Entry"

	je_date_cond = date_cond.replace("pe.posting_date", "je.posting_date")
	je_rows = frappe.db.sql(
		f"""
		SELECT je.name, je.posting_date, je.cheque_no AS reference_no,
			SUM(jea.debit_in_account_currency) - SUM(jea.credit_in_account_currency) AS amount
		FROM `tabJournal Entry` je
		INNER JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
		WHERE je.docstatus = 1 AND je.company = %(company)s AND jea.account = %(account)s
			{je_date_cond}
		GROUP BY je.name
		HAVING amount > 0
		ORDER BY je.posting_date ASC
		""",
		values,
		as_dict=True,
	)
	for r in je_rows:
		r["voucher_type"] = "Journal Entry"
		r["party_type"] = None
		r["party"] = None
		r["party_name"] = None
		r["mode_of_payment"] = None

	entries = sorted(pe_rows + je_rows, key=lambda r: (r["posting_date"], r["name"]))
	total = sum(flt(r["amount"]) for r in entries)
	return {"entries": entries, "summary": {"count": len(entries), "total": total}}


def _je_gl_entries(company, journal_entry):
	if not journal_entry:
		return []
	return frappe.get_all(
		"GL Entry",
		filters={
			"voucher_type": "Journal Entry",
			"voucher_no": journal_entry,
			"company": company,
			"is_cancelled": 0,
		},
		fields=[
			"name",
			"posting_date",
			"account",
			"party_type",
			"party",
			"debit",
			"credit",
			"against",
			"voucher_type",
			"voucher_no",
		],
		order_by="debit desc, account asc",
	)


@frappe.whitelist()
def get_clearing_sweep_gl_entries(name: str):
	"""GL Entries for one submitted Forms Clearing Sweep's Journal Entry —
	for the Clearing History table's "GL Entries" link."""
	ensure_admin()
	company = get_selected_company()
	doc = frappe.get_doc("Forms Clearing Sweep", name)
	if not company or doc.company != company:
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	if doc.docstatus != 1:
		frappe.throw(_("Only submitted clearing sweeps have GL Entries."))
	return _je_gl_entries(doc.company, doc.journal_entry)


@frappe.whitelist()
def process_clearing_sweep(
	clearing_account: str,
	expense_account: str,
	posting_date: str = "",
	amount: float = 0,
	remark: str = "",
):
	"""Create and submit one Forms Clearing Sweep: posts a Journal Entry
	Debit <expense_account> / Credit <clearing_account> for <amount>, and
	remembers this account pair for the company. The amount, and the balance
	it's checked against, are recomputed server-side in the doctype's own
	validate() — a stale or tampered client value can't get through.
	"""
	ensure_admin()
	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	company = _require_company()
	posting_date = getdate(posting_date) if posting_date else getdate(nowdate())

	doc = frappe.get_doc(
		{
			"doctype": "Forms Clearing Sweep",
			"company": company,
			"clearing_account": clearing_account,
			"expense_account": expense_account,
			"posting_date": posting_date,
			"amount": flt(amount),
			"remark": (remark or "").strip() or None,
		}
	)
	try:
		doc.insert()
		doc.submit()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Clearing sweep failed")
		raise

	_save_clearing_settings(company, clearing_account, expense_account)
	frappe.db.commit()

	return {
		"name": doc.name,
		"journal_entry": doc.journal_entry,
		"amount": doc.amount,
		"clearing_balance": doc.clearing_balance,
		"gl_entries": _je_gl_entries(company, doc.journal_entry),
	}


@frappe.whitelist()
def get_clearing_history(from_date: str = "", to_date: str = "", doc_no_search: str = ""):
	"""Recent clearing sweeps (submitted or cancelled) for the selected
	company. from_date/to_date narrow by posting_date, doc_no_search by a
	substring of the document number — same filter shape as the deposit and
	sales history tables.
	"""
	ensure_admin()
	company = get_selected_company()
	if not company:
		return []
	filters = [["docstatus", "in", [1, 2]], ["company", "=", company]]
	if from_date:
		filters.append(["posting_date", ">=", from_date])
	if to_date:
		filters.append(["posting_date", "<=", to_date])
	if doc_no_search:
		filters.append(["name", "like", f"%{doc_no_search}%"])

	sweeps = frappe.get_all(
		"Forms Clearing Sweep",
		filters=filters,
		fields=[
			"name",
			"posting_date",
			"clearing_account",
			"expense_account",
			"clearing_balance",
			"amount",
			"remark",
			"journal_entry",
			"docstatus",
			"creation",
			"owner",
		],
		order_by="posting_date desc, creation desc",
		limit_page_length=50,
	)

	full_name_by_owner = {}
	owners = {s.owner for s in sweeps if s.owner}
	if owners:
		full_name_by_owner = {
			row.name: row.full_name
			for row in frappe.get_all(
				"User", filters={"name": ["in", list(owners)]}, fields=["name", "full_name"]
			)
		}
	for s in sweeps:
		s["created_by"] = full_name_by_owner.get(s.owner) or s.owner

	return sweeps


@frappe.whitelist()
def cancel_clearing_sweep(name: str):
	"""Cancel a clearing sweep made through this page — a plain doc.cancel();
	FormsClearingSweep.on_cancel() cancels its linked Journal Entry itself.
	"""
	ensure_admin()
	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	doc = frappe.get_doc("Forms Clearing Sweep", name)
	if doc.docstatus != 1:
		frappe.throw(_("{0} is not a submitted clearing sweep.").format(name))

	company = get_selected_company()
	if company and doc.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(name, company))

	try:
		doc.cancel()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Clearing sweep cancel failed")
		raise

	return {"name": doc.name}

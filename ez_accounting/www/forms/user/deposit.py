# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

# Same fallback ERPNext's own resolution uses when nothing more specific is
# set: the Company's own default account, or the single account of that type
# if there's exactly one — reused here (rather than reimplemented) for the
# Bank Account default, and as the last resort for Cash/Checks Account too.
from erpnext.accounts.doctype.journal_entry.journal_entry import get_default_bank_cash_account

from ez_accounting.api import ensure_admin, get_selected_company
from ez_accounting.ez_accounting.doctype.forms_bank_deposit_sheet.forms_bank_deposit_sheet import (
	get_cash_payment_entries,
	get_checks_payment_entries,
	get_expected_cash,
	get_expected_checks,
	get_undeposited_payment_entries as _get_undeposited_payment_entries,
)

no_cache = 1


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "deposit"
	context.title = _("Bank Deposit Sheet")
	context.company = get_selected_company()
	context.amend_from = ""
	amend_from = (frappe.form_dict.get("amend") or "").strip()
	if amend_from and _is_amendable_source(amend_from, context.company):
		context.amend_from = amend_from
	return context


def _is_amendable_source(name: str, company: str | None) -> bool:
	doc = frappe.db.get_value("Forms Bank Deposit Sheet", name, ["docstatus", "company"], as_dict=True)
	if not doc or doc.docstatus != 2 or (company and doc.company != company):
		return False
	return not frappe.db.exists("Forms Bank Deposit Sheet", {"amended_from": name})


@frappe.whitelist()
def get_amend_form_data(name: str):
	ensure_admin()
	company = get_selected_company()
	if not _is_amendable_source(name, company):
		frappe.throw(_("{0} is not a cancelled deposit sheet available for amendment.").format(name))
	doc = frappe.get_doc("Forms Bank Deposit Sheet", name)
	return {field: doc.get(field) for field in (
		"name", "company", "deposit_to_account", "deposit_date", "from_receipt_date",
		"to_receipt_date", "cash_account", "bank_account", "checks_account", "usd100",
		"usd50", "usd20", "usd10", "usd5", "usd1", "coins", "check_amount",
	)}


def _resolve_default_account(company: str, account_type: str, mode_keyword: str) -> str | None:
	"""Best-guess default for the Cash/Checks Account field: whichever account
	this company's Mode of Payment Account configuration actually uses for a
	mode matching `mode_keyword` (e.g. "Cash" or "Check") — the same real
	configuration `_get_expected_amount()` (forms_bank_deposit_sheet.py)
	already relies on to classify Payment Entries, so the guessed default and
	the reconciliation math can't disagree about which account "is" the cash
	or checks account. Falls back to ERPNext's own generic default (Company's
	own default account for that type, or the single account of that type if
	there's exactly one) when no matching Mode of Payment is configured.

	A company can have more than one mode matching mode_keyword pointing at
	*different* accounts (e.g. EIS has both "Check - Inbound" -> its bank
	checking account, and "Check - Undeposited" -> its Undeposited Funds
	account) — picking the wrong one as the default silently drops every
	Payment Entry paid into the other account from this page's preview/list
	(the selected account narrows the reconciliation query, see
	_get_payment_entries() in forms_bank_deposit_sheet.py). When there's a
	choice, prefer whichever account's name contains "Undeposited": that's
	exactly the staging concept this whole page reconciles against, so it's
	the more useful default even when it's not alphabetically first.
	"""
	row = frappe.db.sql(
		"""
		SELECT mopa.default_account
		FROM `tabMode of Payment Account` mopa
		INNER JOIN `tabMode of Payment` mop ON mop.name = mopa.parent
		WHERE mopa.company = %(company)s AND mop.name LIKE %(mode_keyword)s
		ORDER BY (mopa.default_account NOT LIKE '%%Undeposited%%'), mop.name
		LIMIT 1
		""",
		{"company": company, "mode_keyword": f"%{mode_keyword}%"},
	)
	if row and row[0][0]:
		return row[0][0]

	fallback = get_default_bank_cash_account(company, account_type, fetch_balance=False)
	return fallback.get("account") if fallback else None


@frappe.whitelist()
def get_accounts():
	"""Cash- and Bank-type leaf accounts for the selected company, for the Cash
	Account / Bank Account / Checks Account fields, plus a best-guess default
	for each — Checks Account draws from both types since a company may stage
	checks in a Cash-type "Undeposited Funds" account, or deposit them
	straight into the Bank checking account. Defaults are suggestions only;
	the client still lets the admin pick something else before submitting.
	"""
	ensure_admin()
	company = get_selected_company()
	if not company:
		return {
			"cash_accounts": [],
			"bank_accounts": [],
			"checks_accounts": [],
			"deposit_to_accounts": [],
			"default_cash_account": None,
			"default_bank_account": None,
			"default_checks_account": None,
			"default_deposit_to_account": None,
		}

	cash_accounts = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Cash", "is_group": 0},
		fields=["name"],
		order_by="name asc",
	)
	bank_accounts = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Bank", "is_group": 0},
		fields=["name"],
		order_by="name asc",
	)
	checks_accounts = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": ["in", ["Cash", "Bank"]], "is_group": 0},
		fields=["name"],
		order_by="name asc",
	)

	bank_default = get_default_bank_cash_account(company, "Bank", fetch_balance=False)

	return {
		"cash_accounts": cash_accounts,
		"bank_accounts": bank_accounts,
		"checks_accounts": checks_accounts,
		# "Deposit to" (the receipt filter, and where this deposit lands) is
		# always a real Bank-type account — same list as Bank Account above.
		"deposit_to_accounts": bank_accounts,
		"default_deposit_to_account": bank_default.get("account") if bank_default else None,
		"default_cash_account": _resolve_default_account(company, "Cash", "Cash"),
		"default_bank_account": bank_default.get("account") if bank_default else None,
		# Checks fall back to "Bank" (not "Cash") when no Mode of Payment is
		# configured — a company with no "X - Undeposited" staging account set
		# up most likely deposits checks straight into the bank, same as the
		# Bank Account default.
		"default_checks_account": _resolve_default_account(company, "Bank", "Check"),
	}


@frappe.whitelist()
def get_expected_cash_preview(
	from_receipt_date: str, to_receipt_date: str, cash_account: str = "", deposit_to_account: str = ""
):
	"""Preview endpoint for the form: the same calculation
	process_express_deposit() (via Forms Bank Deposit Sheet's own validate())
	uses, so the preview can't drift from what actually gets saved.
	"""
	ensure_admin()

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))
	if not from_receipt_date or not to_receipt_date:
		frappe.throw(_("From Receipt Date and To Receipt Date are required."))

	expected = get_expected_cash(
		company,
		getdate(from_receipt_date),
		getdate(to_receipt_date),
		cash_account or None,
		deposit_to_account or None,
	)
	return {"expected_cash": expected}


@frappe.whitelist()
def get_expected_checks_preview(
	from_receipt_date: str, to_receipt_date: str, checks_account: str = "", deposit_to_account: str = ""
):
	"""Preview endpoint for the form: the same calculation
	process_express_deposit() (via Forms Bank Deposit Sheet's own validate())
	uses, so the preview can't drift from what actually gets saved.
	"""
	ensure_admin()

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))
	if not from_receipt_date or not to_receipt_date:
		frappe.throw(_("From Receipt Date and To Receipt Date are required."))

	expected = get_expected_checks(
		company,
		getdate(from_receipt_date),
		getdate(to_receipt_date),
		checks_account or None,
		deposit_to_account or None,
	)
	return {"expected_checks": expected}


@frappe.whitelist()
def get_payment_entries_preview(
	from_receipt_date: str,
	to_receipt_date: str,
	cash_account: str = "",
	checks_account: str = "",
	deposit_to_account: str = "",
):
	"""The individual Payment Entries behind the System Expected Cash/Checks
	previews above, so the admin can see *what* is being totaled for the
	selected range before submitting — not just the final numbers. Built from
	the exact same Forms Bank Deposit Sheet helpers those previews call
	(get_cash_payment_entries/get_checks_payment_entries share their filtering
	with get_expected_cash/get_expected_checks), so this list and those totals
	can never disagree. Note this shows *all* matching entries for the range,
	not "expected minus already deposited" — the previews already show the
	post-deduction figure; this is the raw list explaining it.
	"""
	ensure_admin()

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))
	if not from_receipt_date or not to_receipt_date:
		frappe.throw(_("From Receipt Date and To Receipt Date are required."))

	from_date = getdate(from_receipt_date)
	to_date = getdate(to_receipt_date)

	cash_entries = get_cash_payment_entries(
		company, from_date, to_date, cash_account or None, deposit_to_account or None
	)
	checks_entries = get_checks_payment_entries(
		company, from_date, to_date, checks_account or None, deposit_to_account or None
	)

	for row in cash_entries:
		row["bucket"] = "Cash"
	for row in checks_entries:
		row["bucket"] = "Check"

	entries = sorted(cash_entries + checks_entries, key=lambda row: (row["posting_date"], row["name"]))

	total_cash = sum(flt(row["paid_amount"]) for row in cash_entries)
	total_checks = sum(flt(row["paid_amount"]) for row in checks_entries)

	return {
		"entries": entries,
		"summary": {
			"count": len(entries),
			"cash_count": len(cash_entries),
			"checks_count": len(checks_entries),
			"total_cash": total_cash,
			"total_checks": total_checks,
			"total": total_cash + total_checks,
		},
	}


@frappe.whitelist()
def get_undeposited_payment_entries(deposit_to_account: str = ""):
	"""Every cash/check receipt that no submitted deposit sheet covers yet —
	the "still to deposit" list shown at the top of the page, before a date
	range is even chosen. Scoped to the selected Deposit to account when one
	is picked (same item-binding filter as the rest of the page), otherwise
	every undeposited receipt for the company.
	"""
	ensure_admin()

	company = get_selected_company()
	empty = {
		"entries": [],
		"summary": {
			"count": 0,
			"cash_count": 0,
			"checks_count": 0,
			"total_cash": 0,
			"total_checks": 0,
			"total": 0,
			"earliest": None,
			"latest": None,
		},
	}
	if not company:
		return empty

	entries = _get_undeposited_payment_entries(company, deposit_to_account or None)
	if not entries:
		return empty

	cash = [e for e in entries if e["bucket"] == "Cash"]
	checks = [e for e in entries if e["bucket"] == "Check"]
	total_cash = sum(flt(e["paid_amount"]) for e in cash)
	total_checks = sum(flt(e["paid_amount"]) for e in checks)
	dates = sorted(e["posting_date"] for e in entries if e["posting_date"])

	return {
		"entries": entries,
		"summary": {
			"count": len(entries),
			"cash_count": len(cash),
			"checks_count": len(checks),
			"total_cash": total_cash,
			"total_checks": total_checks,
			"total": total_cash + total_checks,
			"earliest": str(dates[0]) if dates else None,
			"latest": str(dates[-1]) if dates else None,
		},
	}


def _get_deposit_gl_entries(
	company,
	from_receipt_date,
	to_receipt_date,
	cash_account,
	checks_account,
	deposit_journal_entry=None,
	deposit_to_account=None,
):
	"""GL Entries behind a deposit sheet, from both sides of the story: the
	Payment Entries it covers (same voucher set get_payment_entries_preview()
	lists and the same totals get_expected_cash/get_expected_checks reconcile
	against, so "what the deposit sheet says" and "what hit the books" can't
	disagree) — money arriving into Undeposited Funds/Cash — plus, when given,
	the deposit sheet's own Journal Entry (FormsBankDepositSheet._post_deposit_journal_entry())
	moving that same money out of Undeposited Funds/Cash and into the Bank
	Account. Together these are the full picture of a deposit's GL footprint.
	"""
	cash_entries = get_cash_payment_entries(
		company, from_receipt_date, to_receipt_date, cash_account or None, deposit_to_account or None
	)
	checks_entries = get_checks_payment_entries(
		company, from_receipt_date, to_receipt_date, checks_account or None, deposit_to_account or None
	)
	voucher_names = sorted({e["name"] for e in cash_entries} | {e["name"] for e in checks_entries})

	fields = [
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
	]

	entries = []
	if voucher_names:
		entries += frappe.get_all(
			"GL Entry",
			filters={
				"voucher_type": "Payment Entry",
				"voucher_no": ["in", voucher_names],
				"company": company,
				"is_cancelled": 0,
			},
			fields=fields,
			order_by="voucher_no asc, debit desc, account asc",
		)
	if deposit_journal_entry:
		entries += frappe.get_all(
			"GL Entry",
			filters={
				"voucher_type": "Journal Entry",
				"voucher_no": deposit_journal_entry,
				"company": company,
				"is_cancelled": 0,
			},
			fields=fields,
			order_by="debit desc, account asc",
		)

	return entries


@frappe.whitelist()
def get_deposit_gl_entries(name: str):
	"""GL Entries for one already-submitted Forms Bank Deposit Sheet, keyed off
	its stored date range/accounts/journal entry rather than a snapshot list of
	vouchers (the doctype doesn't store the Payment Entry names, only the JE) —
	used by the Deposit History table's "GL Entries" link.
	"""
	ensure_admin()
	company = get_selected_company()
	doc = frappe.get_doc("Forms Bank Deposit Sheet", name)
	if not company or doc.company != company:
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	if doc.docstatus != 1:
		frappe.throw(_("Only submitted deposit sheets have GL Entries."))

	return _get_deposit_gl_entries(
		doc.company,
		doc.from_receipt_date,
		doc.to_receipt_date,
		doc.cash_account,
		doc.checks_account,
		doc.deposit_journal_entry,
		doc.deposit_to_account,
	)


@frappe.whitelist()
def get_deposit_history(
	from_date: str = "", to_date: str = "", doc_no_search: str = "", customer_search: str = ""
):
	"""Recent deposit sheets (submitted or cancelled — drafts don't belong
	here, this page never leaves one behind) so the admin can see what's
	already been recorded before starting a new one, without leaving this page.

	from_date/to_date optionally narrow by deposit_date (either end
	independently optional); doc_no_search optionally narrows by a substring
	of the deposit sheet's document number (its name, e.g.
	"BDS-2026-09-01-00040") — all server-side, same shape as sales.py's
	get_sales_invoice_history(), so a match outside the default 50-row window
	is still found rather than silently hidden.
	"""
	ensure_admin()
	company = get_selected_company()
	if not company:
		return []
	filters = [["docstatus", "in", [1, 2]], ["company", "=", company]]
	if from_date:
		filters.append(["deposit_date", ">=", from_date])
	if to_date:
		filters.append(["deposit_date", "<=", to_date])
	if doc_no_search:
		filters.append(["name", "like", f"%{doc_no_search}%"])
	if customer_search:
		# Sheets that cover at least one Payment Entry from a matching customer
		# (party ID or name), via the per-receipt custom_deposit_sheet flag.
		like = f"%{customer_search}%"
		matched = frappe.get_all(
			"Payment Entry",
			filters={"custom_deposit_sheet": ["is", "set"], "party_type": "Customer", "docstatus": 1},
			or_filters=[["party", "like", like], ["party_name", "like", like]],
			pluck="custom_deposit_sheet",
		)
		filters.append(["name", "in", list(set(matched)) or [""]])
	sheets = frappe.get_all(
		"Forms Bank Deposit Sheet",
		filters=filters,
		fields=[
			"name",
			"company",
			"deposit_to_account",
			"deposit_date",
			"from_receipt_date",
			"to_receipt_date",
			"total_deposit",
			"total_variance",
			"docstatus",
			"deposit_journal_entry",
			"creation",
			"owner",
		],
		order_by="deposit_date desc, creation desc",
		limit_page_length=50,
	)

	# owner is the creating user's email/ID, not something an admin wants to
	# read at a glance — resolved to Full Name in one batch query rather than
	# per-row, same reasoning as sales.py's get_sales_invoice_history().
	full_name_by_owner = {}
	owners = {s.owner for s in sheets if s.owner}
	if owners:
		full_name_by_owner = frappe.get_all(
			"User", filters={"name": ["in", list(owners)]}, fields=["name", "full_name"], as_list=False
		)
		full_name_by_owner = {row.name: row.full_name for row in full_name_by_owner}

	for s in sheets:
		s["created_by"] = full_name_by_owner.get(s.owner) or s.owner

	return sheets


@frappe.whitelist()
def cancel_express_deposit(name: str):
	"""Cancel a deposit sheet made through this page — a plain `doc.cancel()`
	is enough (unlike Express Sales' own cancel_express_sale(), which has to
	cancel a separate Payment Entry first): FormsBankDepositSheet.on_cancel()
	already cancels its own linked Journal Entry itself, so a deposit is never
	left half-cancelled the same way a sale's Payment Entry could be if
	cancelled out of order.
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	doc = frappe.get_doc("Forms Bank Deposit Sheet", name)
	if doc.docstatus != 1:
		frappe.throw(_("{0} is not a submitted deposit sheet.").format(name))

	company = get_selected_company()
	if company and doc.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(name, company))

	try:
		doc.cancel()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Deposit cancel failed")
		raise

	return {"name": doc.name}


@frappe.whitelist()
def cancel_deposit_and_undeposit_invoices(name: str):
	"""Cancel a submitted deposit sheet and release every Payment Entry
	tagged to it back to the undeposited state. The Payment Entries and their
	invoices remain submitted; FormsBankDepositSheet.on_cancel() clears the
	deposit stamp and cancels the deposit Journal Entry.
	"""
	ensure_admin()

	doc = frappe.get_doc("Forms Bank Deposit Sheet", name)
	if doc.docstatus != 1:
		frappe.throw(_("{0} is not a submitted deposit sheet.").format(name))

	company = get_selected_company()
	if company and doc.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(name, company))

	payment_entries = frappe.get_all(
		"Payment Entry",
		filters={"custom_deposit_sheet": name, "docstatus": 1},
		pluck="name",
	)

	try:
		doc.cancel()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Cancel deposit and undeposit invoices failed")
		raise

	return {"name": doc.name, "undeposited_payment_entries": payment_entries}


@frappe.whitelist()
def process_express_deposit(
	deposit_date: str,
	from_receipt_date: str,
	to_receipt_date: str,
	cash_account: str,
	bank_account: str,
	checks_account: str,
	deposit_to_account: str,
	usd100: int = 0,
	usd50: int = 0,
	usd20: int = 0,
	usd10: int = 0,
	usd5: int = 0,
	usd1: int = 0,
	coins: float = 0,
	check_amount: float = 0,
	amended_from: str = "",
):
	"""Create and submit one Forms Bank Deposit Sheet. A physical cash/check
	count against what the system expected for the given receipt period, that
	also posts the real accounting for it on submit: FormsBankDepositSheet.on_submit()
	moves the physically-counted amount from the Cash/Checks Account (usually
	an Undeposited Funds staging account) into the Bank Account via a Journal
	Entry. Total Physical Cash / System Expected Cash / Variance (and their
	check-amount equivalents) are never taken from the client — Forms Bank
	Deposit Sheet.validate() recomputes all of them from the denomination
	counts, check amount, and live data every time, so a tampered or stale
	client-side total can't end up on the saved record (or on the Journal
	Entry, which is built from those same server-computed fields).
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))
	amended_from = (amended_from or "").strip()
	if amended_from and not _is_amendable_source(amended_from, company):
		frappe.throw(_("{0} is not a cancelled deposit sheet available for amendment.").format(amended_from))

	if not cash_account or not frappe.db.exists(
		"Account", {"name": cash_account, "company": company, "account_type": "Cash", "is_group": 0}
	):
		frappe.throw(_("{0} is not an active Cash account for {1}.").format(cash_account, company))
	if not bank_account or not frappe.db.exists(
		"Account", {"name": bank_account, "company": company, "account_type": "Bank", "is_group": 0}
	):
		frappe.throw(_("{0} is not an active Bank account for {1}.").format(bank_account, company))
	if not checks_account or not frappe.db.exists(
		"Account",
		{"name": checks_account, "company": company, "account_type": ["in", ["Cash", "Bank"]], "is_group": 0},
	):
		frappe.throw(_("{0} is not an active account for {1}.").format(checks_account, company))
	if not deposit_to_account or not frappe.db.exists(
		"Account", {"name": deposit_to_account, "company": company, "account_type": "Bank", "is_group": 0}
	):
		frappe.throw(_("Select a valid Deposit to account for {0}.").format(company))

	deposit_date = getdate(deposit_date) if deposit_date else getdate(nowdate())
	from_receipt_date = getdate(from_receipt_date)
	to_receipt_date = getdate(to_receipt_date)
	if to_receipt_date < from_receipt_date:
		frappe.throw(_("To Receipt Date cannot be before From Receipt Date."))

	doc = frappe.get_doc(
		{
			"doctype": "Forms Bank Deposit Sheet",
			"company": company,
			"deposit_to_account": deposit_to_account,
			"deposit_date": deposit_date,
			"from_receipt_date": from_receipt_date,
			"to_receipt_date": to_receipt_date,
			"cash_account": cash_account,
			"bank_account": bank_account,
			"checks_account": checks_account,
			"usd100": usd100,
			"usd50": usd50,
			"usd20": usd20,
			"usd10": usd10,
			"usd5": usd5,
			"usd1": usd1,
			"coins": coins,
			"check_amount": check_amount,
			"amended_from": amended_from or None,
		}
	)
	try:
		# A deposit may only be posted when the physical count reconciles
		# exactly with the system expectation. validate() recomputes this from
		# the submitted counts and live Payment Entries, so the rule cannot be
		# bypassed by changing the browser preview.
		doc.validate()
		if abs(flt(doc.total_variance)) > 0.000001:
			frappe.throw(_("Cannot submit a deposit with a Total Variance of {0}.").format(doc.total_variance))
		doc.insert()
		doc.submit()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Deposit failed")
		raise

	gl_entries = _get_deposit_gl_entries(
		company,
		from_receipt_date,
		to_receipt_date,
		cash_account,
		checks_account,
		doc.deposit_journal_entry,
		deposit_to_account,
	)

	return {
		"name": doc.name,
		"total_physical_cash": doc.total_physical_cash,
		"system_expected_cash": doc.system_expected_cash,
		"variance": doc.variance,
		"check_amount": doc.check_amount,
		"system_expected_checks": doc.system_expected_checks,
		"check_variance": doc.check_variance,
		"total_deposit": doc.total_deposit,
		"total_expected": doc.total_expected,
		"total_variance": doc.total_variance,
		"deposit_journal_entry": doc.deposit_journal_entry,
		"gl_entries": gl_entries,
	}

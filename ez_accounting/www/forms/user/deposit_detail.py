# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt

from ez_accounting.api import ensure_admin, get_selected_company, permitted_docs
from ez_accounting.www.forms.user.deposit import _get_deposit_gl_entries

no_cache = 1

# US bill/coin rows on the sheet, in the order they're counted. (fieldname, face value)
DENOMINATIONS = [
	("usd100", 100),
	("usd50", 50),
	("usd20", 20),
	("usd10", 10),
	("usd5", 5),
	("usd1", 1),
]

STATUS_LABEL = {0: "Draft", 1: "Submitted", 2: "Cancelled"}


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "deposit"
	context.title = _("Deposit Detail")
	context.sheet = None
	context.not_found = False
	context.forbidden = False

	name = (frappe.form_dict.get("id") or "").strip()
	if not name or not frappe.db.exists("Forms Bank Deposit Sheet", name):
		context.not_found = True
		context.title = _("Deposit Detail — not found")
		return context

	doc = frappe.get_doc("Forms Bank Deposit Sheet", name)

	# The real access check: a company-restricted user (ERPNext User Permission)
	# may only open a sheet for a company they're permitted on. Unrestricted
	# users (the common case) see any. Deliberately not gated to the *currently
	# selected* company — this is a deep link from the Sales page, and the row
	# it came from is already for a company the user can see.
	permitted = permitted_docs("Company")
	if permitted is not None and doc.company not in permitted:
		context.forbidden = True
		context.title = _("Deposit Detail — not permitted")
		return context

	context.title = _("Deposit {0}").format(doc.name)
	context.selected_company = get_selected_company()

	denominations = []
	for fieldname, face in DENOMINATIONS:
		count = int(doc.get(fieldname) or 0)
		if count:
			denominations.append({"label": f"${face}", "count": count, "amount": count * face})
	coins = flt(doc.coins)

	receipts = frappe.get_all(
		"Payment Entry",
		filters={"custom_deposit_sheet": doc.name, "docstatus": 1},
		fields=[
			"name",
			"posting_date",
			"party_type",
			"party",
			"paid_amount",
			"mode_of_payment",
			"reference_no",
			"reference_date",
			"paid_to",
		],
		order_by="posting_date asc, name asc",
	)

	gl_entries = []
	if doc.docstatus == 1:
		gl_entries = _get_deposit_gl_entries(
			doc.company,
			doc.from_receipt_date,
			doc.to_receipt_date,
			doc.cash_account,
			doc.checks_account,
			doc.deposit_journal_entry,
			doc.deposit_to_account,
		)

	context.sheet = doc
	context.status_label = STATUS_LABEL.get(doc.docstatus, "Draft")
	context.denominations = denominations
	context.coins = coins
	context.receipts = receipts
	context.receipts_total = sum(flt(r.paid_amount) for r in receipts)
	context.gl_entries = gl_entries
	context.gl_total_debit = sum(flt(e.debit) for e in gl_entries)
	context.gl_total_credit = sum(flt(e.credit) for e in gl_entries)
	return context

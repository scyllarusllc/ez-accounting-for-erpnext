# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

# ERPNext's own helper for turning a Sales/Purchase Invoice into a matching
# Payment Entry (party, party account, paid/received amounts, exchange rates
# all resolved from the invoice) — reused instead of hand-building a Payment
# Entry so this stays in sync with however ERPNext computes those on upgrade,
# same reasoning as Express Payroll reusing HRMS's own Payroll Entry flow.
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

# Same resolution ERPNext's own Payment Entry falls back to when no account is
# explicitly given: the Company's own default bank account, or the single
# Bank-type Account for that company if there's exactly one.
from erpnext.accounts.doctype.journal_entry.journal_entry import get_default_bank_cash_account

from ez_accounting.api import (
	apply_permitted_filter,
	ensure_admin,
	get_selected_company,
	is_company_restricted,
	upsert_party_default,
)

no_cache = 1


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "purchase"
	context.title = _("Express Purchase")
	context.company = get_selected_company()
	return context


@frappe.whitelist()
def get_suppliers():
	ensure_admin()
	return frappe.get_all(
		"Supplier",
		filters=apply_permitted_filter({"disabled": 0}, "Supplier"),
		fields=["name", "supplier_name"],
		order_by="supplier_name asc",
	)


@frappe.whitelist()
def get_purchase_items():
	"""Active, purchasable Items only. Unlike Express Payroll/Sales' items, no
	standard_rate is included — that field is a *selling* price; there's no
	equivalent single "buying price" on the Item itself (real purchase prices
	vary by supplier, in Supplier Item Price), so Rate is left blank for the
	admin to fill in from the actual bill.
	"""
	ensure_admin()
	return frappe.get_all(
		"Item",
		filters=apply_permitted_filter({"disabled": 0, "is_purchase_item": 1}, "Item"),
		fields=["name", "item_name", "stock_uom"],
		order_by="item_name asc",
	)


@frappe.whitelist()
def get_modes_of_payment():
	ensure_admin()
	return frappe.get_all(
		"Mode of Payment",
		filters={"enabled": 1},
		fields=["name"],
		order_by="name asc",
	)


def _resolve_paid_from_account(company: str, mode_of_payment: str = "") -> str | None:
	"""Which GL account a purchase's payment actually gets paid from: the Mode
	of Payment's own configured account for this company (Mode of Payment
	Account) when it has one, otherwise the same fallback ERPNext's own
	Payment Entry resolution uses when nothing more specific is set — the
	Company's default bank account, or the single Bank-type Account for that
	company if there's exactly one. Shared by the preview endpoint and the
	real submit path so they can never drift apart.
	"""
	if mode_of_payment:
		mode_account = frappe.db.get_value(
			"Mode of Payment Account", {"parent": mode_of_payment, "company": company}, "default_account"
		)
		if mode_account:
			return mode_account

	bank = get_default_bank_cash_account(company, "Bank", fetch_balance=False)
	return bank.get("account") if bank else None


@frappe.whitelist()
def get_pay_from_preview(mode_of_payment: str = ""):
	"""Preview endpoint for the form: resolves (without touching any document)
	which account a purchase would actually be paid from right now, so the
	admin can see it before submitting — same resolution
	process_express_purchase() itself uses, via _resolve_paid_from_account().
	"""
	ensure_admin()

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	return {"account": _resolve_paid_from_account(company, mode_of_payment)}


@frappe.whitelist()
def set_default_bank_account():
	"""Set the selected Company's first usable Bank ledger as its default.

	This is the one-click recovery offered by Express Purchase when neither a
	Mode of Payment account nor a Company default can resolve Pay From.
	"""
	ensure_admin()
	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	current = frappe.db.get_value("Company", company, "default_bank_account")
	if current and frappe.db.exists(
		"Account", {"name": current, "company": company, "account_type": "Bank", "is_group": 0, "disabled": 0}
	):
		return {"account": current, "created": False}

	account = frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Bank", "is_group": 0, "disabled": 0},
		"name",
		order_by="lft asc",
	)
	if not account:
		frappe.throw(_("No active Bank account exists for {0}. Create one in the Chart of Accounts first.").format(company))

	frappe.db.set_value("Company", company, "default_bank_account", account, update_modified=True)
	frappe.db.commit()
	return {"account": account, "created": True}


@frappe.whitelist()
def get_expense_account_preview(supplier: str, item: str):
	"""Preview endpoint for the form: given a supplier + item, resolve and
	return the Expense Account a real purchase would post to — via
	set_missing_values() on an unsaved, never-inserted Purchase Invoice, the
	same resolution submit itself uses (Item Default -> Item Group Default ->
	Company default), so the preview can never drift from what actually gets
	posted. Supplier is required even though expense-account resolution
	itself doesn't depend on it — set_missing_values() also resolves the
	party's currency/exchange rate and throws without one.
	"""
	ensure_admin()

	if not supplier or not item:
		frappe.throw(_("Supplier and Item are required."))

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	pi = frappe.get_doc(
		{
			"doctype": "Purchase Invoice",
			"supplier": supplier,
			"company": company,
			"posting_date": nowdate(),
			"items": [{"item_code": item, "qty": 1, "rate": 0}],
		}
	)
	pi.set_missing_values(for_validate=True)

	return {"expense_account": pi.items[0].expense_account}


@frappe.whitelist()
def get_purchase_invoice_history(
	from_date: str = "", to_date: str = "", supplier_search: str = "", item_search: str = ""
):
	"""Recent Purchase Invoices (submitted or cancelled — drafts don't belong
	here, this page never leaves one behind) so the admin can see what's already
	been recorded before starting a new one, without leaving this page. Each row
	also carries the submitted Payment Entry paying it off (if any) and that
	entry's Mode of Payment — a purchase made via "Full Payment" will have
	exactly one; a "Partial Payment"/"Invoice Only" purchase followed by one or
	more later "Pay" payments may have several (only the first is shown here;
	see get_purchase_invoice_payment_history() for the full trail). from_date/
	to_date optionally narrow by posting_date, either end independently
	optional. Mirrors sales.py's get_sales_invoice_history(), including its
	supplier_search/item_search server-side filters (customer -> supplier).
	"""
	ensure_admin()
	filters = [["docstatus", "in", [1, 2]]]
	company = get_selected_company()
	if company:
		filters.append(["company", "=", company])
	elif is_company_restricted():
		# Limited to Companies (User Permission) but none exist on this site.
		return []
	if from_date:
		filters.append(["posting_date", ">=", from_date])
	if to_date:
		filters.append(["posting_date", "<=", to_date])
	if item_search:
		matching_invoices = frappe.get_all(
			"Purchase Invoice Item",
			or_filters=[
				["item_name", "like", f"%{item_search}%"],
				["item_code", "like", f"%{item_search}%"],
			],
			pluck="parent",
			distinct=True,
		)
		if not matching_invoices:
			return []
		filters.append(["name", "in", matching_invoices])

	or_filters = None
	if supplier_search:
		or_filters = [
			["supplier_name", "like", f"%{supplier_search}%"],
			["supplier", "like", f"%{supplier_search}%"],
		]

	invoices = frappe.get_all(
		"Purchase Invoice",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"supplier",
			"supplier_name",
			"posting_date",
			"creation",
			"owner",
			"grand_total",
			"outstanding_amount",
			"currency",
			"status",
		],
		order_by="posting_date desc, creation desc",
		limit_page_length=50,
	)
	if not invoices:
		return invoices

	payment_entries = frappe.get_all(
		"Payment Entry Reference",
		filters={
			"reference_doctype": "Purchase Invoice",
			"reference_name": ["in", [inv.name for inv in invoices]],
			"docstatus": 1,
		},
		fields=["reference_name", "parent"],
	)
	payment_entry_by_invoice = {}
	for ref in payment_entries:
		payment_entry_by_invoice.setdefault(ref.reference_name, ref.parent)

	# paid_from/mode_of_payment on the Payment Entry itself are the actual
	# account and mode the purchase was paid through — same account
	# _resolve_paid_from_account() resolves before payment, just read back off
	# the real submitted record here rather than re-resolved (an invoice's
	# payment could in principle have been recorded outside this page, into a
	# different account/mode).
	pe_by_name = {}
	if payment_entry_by_invoice:
		pe_rows = frappe.get_all(
			"Payment Entry",
			filters={"name": ["in", list(payment_entry_by_invoice.values())]},
			fields=["name", "paid_from", "mode_of_payment", "reference_no"],
		)
		pe_by_name = {row.name: row for row in pe_rows}

	# owner is the creating user's email/ID, not something an admin wants to
	# read at a glance — resolved to Full Name in one batch query rather than
	# per-row, same reasoning as the Payment Entry lookups above.
	full_name_by_owner = {}
	owners = {inv.owner for inv in invoices if inv.owner}
	if owners:
		full_name_by_owner = frappe.get_all(
			"User", filters={"name": ["in", list(owners)]}, fields=["name", "full_name"], as_list=False
		)
		full_name_by_owner = {row.name: row.full_name for row in full_name_by_owner}

	for inv in invoices:
		pe_name = payment_entry_by_invoice.get(inv.name)
		pe_row = pe_by_name.get(pe_name)
		inv["payment_entry"] = pe_name
		inv["pay_from"] = pe_row.paid_from if pe_row else None
		inv["mode_of_payment"] = pe_row.mode_of_payment if pe_row else None
		inv["reference_no"] = pe_row.reference_no if pe_row else None
		inv["created_by"] = full_name_by_owner.get(inv.owner) or inv.owner

	return invoices


@frappe.whitelist()
def get_purchase_invoice_payment_history(purchase_invoice: str):
	"""Every Payment Entry ever recorded against this Purchase Invoice — the
	full trail, not just the single one get_purchase_invoice_history()
	surfaces for its own Pay-From/Mode-of-Payment columns (that function only
	ever picks the first submitted Payment Entry it finds). A purchase can be
	paid off across more than one Payment Entry (Partial Payment at creation,
	followed by one or more later payments via the "Pay" button), so the admin
	needs the whole picture — including any Payment Entry that was later
	cancelled, which the main table's "first submitted" pick would otherwise
	hide entirely. allocated_amount (not the Payment Entry's own paid_amount)
	is what's shown per row — the amount that Payment Entry actually applied
	to *this* invoice, in case it's ever one that also paid off other invoices
	at the same time. Mirrors sales.py's get_sales_invoice_payment_history().
	"""
	ensure_admin()

	invoice_company = frappe.db.get_value("Purchase Invoice", purchase_invoice, "company")
	if not invoice_company:
		frappe.throw(_("{0} is not a valid Purchase Invoice.").format(purchase_invoice))

	company = get_selected_company()
	if company and invoice_company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(purchase_invoice, company))

	references = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Purchase Invoice", "reference_name": purchase_invoice},
		fields=["parent", "allocated_amount"],
	)
	if not references:
		return []

	allocated_by_pe = {row.parent: row.allocated_amount for row in references}

	pe_rows = frappe.get_all(
		"Payment Entry",
		filters={"name": ["in", list(allocated_by_pe.keys())]},
		fields=["name", "posting_date", "mode_of_payment", "reference_no", "docstatus"],
		order_by="posting_date asc, creation asc",
	)

	status_label = {0: _("Draft"), 1: _("Submitted"), 2: _("Cancelled")}
	for row in pe_rows:
		row["allocated_amount"] = allocated_by_pe.get(row.name)
		row["status"] = status_label.get(row.docstatus, "")

	return pe_rows


@frappe.whitelist()
def get_purchase_invoice_for_duplicate(purchase_invoice: str):
	"""Return safe form values for a new purchase based on an existing invoice.

	Document identity, dates, payment references, and submission state are
	deliberately excluded: Duplicate only prepares a new editable form.
	"""
	ensure_admin()

	pi = frappe.get_doc("Purchase Invoice", purchase_invoice)
	company = get_selected_company()
	if company and pi.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(purchase_invoice, company))
	if is_company_restricted() and not company:
		frappe.throw(_("No company is selected."))

	return {
		"source": pi.name,
		"supplier": pi.supplier,
		"remarks": pi.remarks or "",
		"items": [
			{
				"item": row.item_code,
				"qty": abs(flt(row.qty)),
				"rate": abs(flt(row.rate)),
				"description": row.description or "",
				"is_deduction": flt(row.rate) < 0 or flt(row.amount) < 0,
			}
			for row in pi.items
		],
	}


def _parse_line_items(items: str | list) -> list[dict]:
	"""Parse+validate the Item/Qty/Rate rows for a multi-line purchase. Each
	row is checked the same way the old single-item form checked its one row
	— item must be an active purchase Item, qty/rate both > 0 — just looped
	over every row now instead of asserting it once. Returns rows with
	item_doc resolved and qty/rate coerced to float, in the order submitted
	(so the created Purchase Invoice's line order matches what the admin saw
	on screen). Mirrors sales.py's _parse_line_items(), checking
	is_purchase_item instead of is_sales_item.

	`rate` is always the positive magnitude the admin typed — a "Deduction"
	row (is_deduction=True, e.g. a supplier credit/rebate Item) negates it
	here server-side rather than trusting a client-computed signed value, same
	reasoning as every other server-recomputed figure in this app. Qty stays
	strictly positive either way; only the sign of Rate (and so of Amount)
	encodes a deduction.
	"""
	if isinstance(items, str):
		try:
			items = json.loads(items)
		except (TypeError, ValueError):
			frappe.throw(_("Invalid items payload."))

	if not items or not isinstance(items, list):
		frappe.throw(_("At least one item is required."))

	parsed = []
	for row in items:
		item_code = (row.get("item") or "").strip()
		qty = flt(row.get("qty"))
		rate = flt(row.get("rate"))
		description = (row.get("description") or "").strip()
		is_deduction = bool(row.get("is_deduction"))

		if not item_code:
			frappe.throw(_("Every row needs an item."))
		if qty <= 0:
			frappe.throw(_("Quantity must be greater than zero for {0}.").format(item_code))
		if rate <= 0:
			frappe.throw(_("Rate must be greater than zero for {0}.").format(item_code))

		item_doc = frappe.db.get_value(
			"Item", item_code, ["name", "item_name", "disabled", "is_purchase_item"], as_dict=True
		)
		if not item_doc or item_doc.disabled or not item_doc.is_purchase_item:
			frappe.throw(_("{0} is not an active purchase item.").format(item_code))

		parsed.append(
			{
				"item_doc": item_doc,
				"qty": qty,
				"rate": -rate if is_deduction else rate,
				"description": description or None,
			}
		)

	return parsed


@frappe.whitelist()
def process_express_purchase(
	supplier: str,
	items: str | list,
	posting_date: str = "",
	payment_mode: str = "full",
	partial_amount: float = 0,
	mode_of_payment: str = "",
	reference_no: str = "",
	remarks: str = "",
):
	"""Run a purchase end-to-end: Purchase Invoice with one or more lines,
	submitted, then — unless payment_mode is "invoice_only" — a matching
	Payment Entry (via ERPNext's own get_payment_entry) recording that it was
	paid, also submitted. `items` is a JSON list of {item, qty, rate,
	description, is_deduction} rows (description optional; is_deduction
	optional, negates that row's rate — see _parse_line_items()) —
	get_payment_entry() needs no changes at all for multiple lines, since it
	only ever reads the invoice's own outstanding_amount regardless of how
	many rows produced it. Mirrors sales.py's process_express_sale().

	payment_mode is one of:
	  - "full" (default): pay the invoice's full outstanding amount now,
		exactly the original single-mode behavior.
	  - "partial": pay only partial_amount now (must be > 0 and <= the
		invoice's outstanding amount) — the rest stays outstanding on the
		invoice for a later payment.
	  - "invoice_only": create and submit the Purchase Invoice only, no
		Payment Entry at all — for recording a purchase made on credit / to be
		paid later. mode_of_payment/reference_no are meaningless without a
		payment and are ignored in this mode (remarks still reaches the
		invoice either way).

	posting_date defaults to today when left blank — due_date and the Payment
	Entry's reference date both follow it, since this whole flow represents
	one same-day purchase, just not always literally today's date (e.g.
	entering a purchase a day late). Which account payment is paid from is
	resolved by _resolve_paid_from_account() — see there; there's no explicit
	Bank Account field on this form, the resolved account is only shown as a
	preview (get_pay_from_preview()) before submitting.
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	if payment_mode not in ("full", "partial", "invoice_only"):
		frappe.throw(_("Invalid payment mode."))

	line_items = _parse_line_items(items)

	posting_date = getdate(posting_date) if posting_date else getdate(nowdate())

	supplier_doc = frappe.db.get_value(
		"Supplier", supplier, ["name", "supplier_name", "disabled"], as_dict=True
	)
	if not supplier_doc or supplier_doc.disabled:
		frappe.throw(_("{0} is not an active supplier.").format(supplier))

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	# Mode of Payment/Pay-From resolution only matters when a payment is
	# actually going to be recorded this run — skipped entirely for
	# "invoice_only" so a misconfigured (or simply blank) Mode of Payment
	# never blocks recording a credit purchase that was never going to touch a
	# bank account in the first place.
	paid_from_account = None
	if payment_mode != "invoice_only":
		if mode_of_payment and not frappe.db.exists(
			"Mode of Payment", {"name": mode_of_payment, "enabled": 1}
		):
			frappe.throw(_("{0} is not an active Mode of Payment.").format(mode_of_payment))

		paid_from_account = _resolve_paid_from_account(company, mode_of_payment)
		if not paid_from_account:
			frappe.throw(
				_(
					"Could not resolve an account to pay from for {0}. Set a default bank account for"
					" the company, or configure one for the chosen Mode of Payment."
				).format(company)
			)

	try:
		# Step 1 — Purchase Invoice for the lines entered. Expense account,
		# cost center, and payable account are all left for ERPNext's own
		# BuyingController to resolve from Item/Company defaults on
		# insert/submit, rather than looked up and set by hand here.
		pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"supplier": supplier_doc.name,
				"company": company,
				"posting_date": posting_date,
				# Without this, Frappe silently forces posting_date back to today
				# regardless of what's set above — leaving due_date (also set to
				# posting_date, below) stuck in the past and failing "Due Date
				# cannot be before Posting Date".
				"set_posting_time": 1,
				"due_date": posting_date,
				# Populated so the custom remarks the admin types actually reach
				# the invoice itself, not just the Payment Entry — the Invoice
				# print formats ("Purchase Invoice Standard") show this in a
				# Remarks section when non-empty.
				"remarks": remarks.strip() if remarks and remarks.strip() else None,
				"items": [
					{
						"item_code": row["item_doc"].name,
						"qty": row["qty"],
						"rate": row["rate"],
						"description": row["description"],
					}
					for row in line_items
				],
			}
		)
		pi.insert()
		pi.submit()

		pe = None
		if payment_mode != "invoice_only":
			# Step 2 — Payment Entry against the invoice just created, paid
			# from paid_from_account resolved above. payment_type is resolved
			# by get_payment_entry() itself from the doctype ("Pay" for a
			# Purchase Invoice). get_payment_entry() defaults paid_amount (and
			# the one reference row's allocated_amount) to the invoice's full
			# outstanding amount — for "partial", both are overridden below to
			# partial_amount instead, so only that much actually gets applied
			# and the remainder stays outstanding on the invoice for a later
			# payment.
			pe = get_payment_entry("Purchase Invoice", pi.name, bank_account=paid_from_account)

			# get_payment_entry() hardcodes posting_date to nowdate() regardless
			# of the invoice it's built from — override it to match, so a
			# backdated purchase doesn't silently end up with a payment dated
			# today. Left unnoticed, this breaks anything that reconciles by
			# Payment Entry date (e.g. Forms Bank Deposit Sheet's
			# expected-cash/checks calc).
			pe.posting_date = posting_date

			if payment_mode == "partial":
				partial_amount = flt(partial_amount)
				if partial_amount <= 0:
					frappe.throw(_("Enter an amount greater than zero for the partial payment."))
				if partial_amount > pi.outstanding_amount:
					frappe.throw(
						_("The partial payment amount can't be more than the invoice total ({0}).").format(
							frappe.utils.fmt_money(pi.outstanding_amount, currency=pi.currency)
						)
					)
				# Both paid_amount and received_amount need setting (not just
				# one) — this app is single-currency throughout, but Payment
				# Entry still validates paid_amount >= received_amount even
				# when they're meant to be identical (validate_received_
				# amount()), so leaving received_amount at the full-outstanding
				# value get_payment_entry() set would fail that check.
				# allocated_amount on the one reference row has to match too,
				# or Payment Entry's own validation flags the difference as an
				# unallocated advance rather than a partial settlement of this
				# invoice.
				pe.paid_amount = partial_amount
				pe.received_amount = partial_amount
				pe.references[0].allocated_amount = partial_amount

			# Recorded for reference regardless of whether it drove paid_from_account.
			if mode_of_payment:
				pe.mode_of_payment = mode_of_payment

			# Reference No + Reference Date are mandatory whenever either leg of
			# the entry is a Bank-type account (Payment Entry.validate_mandatory(),
			# same constraint Express Payroll hits on its Journal Entry) —
			# default to tying it back to the invoice it's paying off when the
			# admin didn't type an actual cheque/reference number.
			pe.reference_no = reference_no.strip() if reference_no and reference_no.strip() else pi.name
			pe.reference_date = posting_date

			# Remarks: left to Payment Entry's own auto-generated summary unless
			# the admin typed something — custom_remarks has to be set too,
			# otherwise set_remarks() (called from validate()) silently
			# overwrites whatever's in .remarks with its own auto-generated
			# text. custom_line_descriptions is the field that actually matters
			# for print — "Official Receipt with line descriptions" reads THAT
			# field for its per-line remarks text, not .remarks at all; without
			# also setting it, whatever the admin types here never showed up on
			# a printed receipt using that format.
			if remarks and remarks.strip():
				pe.remarks = remarks.strip()
				pe.custom_remarks = 1
				pe.custom_line_descriptions = remarks.strip()

			pe.insert()
			pe.submit()

		# Remember Item/Rate/Mode of Payment/Reference No/Remarks for this
		# supplier so the next Express Purchase for them prefills — inside
		# the same try so a failure here rolls back with everything else
		# rather than silently succeeding on the purchase but not on the
		# memory of it. Forms Party Default only ever remembers one item, so
		# a multi-line purchase remembers its first row (item AND rate,
		# unlike sales.py's version of this call — Purchase items have no
		# standard_rate to fall back on, see get_purchase_items()).
		# mode_of_payment/reference_no are blanked out for "invoice_only" —
		# neither was actually used this run, so remembering them would
		# prefill a payment method for this supplier's next purchase that has
		# nothing to do with how (or whether) this one got paid. The raw
		# reference_no/remarks parameters are passed, not pe.reference_no/
		# pe.remarks — those may hold this invoice's own auto-generated
		# fallback, which would be a wrong thing to prefill next time.
		upsert_party_default(
			"Supplier",
			supplier_doc.name,
			company,
			line_items[0]["item_doc"].name,
			line_items[0]["rate"],
			mode_of_payment if payment_mode != "invoice_only" else "",
			(reference_no.strip() if reference_no and reference_no.strip() else None)
			if payment_mode != "invoice_only"
			else None,
			remarks.strip() if remarks and remarks.strip() else None,
		)

		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Purchase failed")
		raise

	# pe.submit() (above) updates the invoice's outstanding_amount via a
	# direct db_set on the Purchase Invoice row, not through this in-memory pi
	# object — reload so the response reflects the real post-payment
	# outstanding balance (0 for "full", the remainder for "partial", the
	# full grand_total for "invoice_only") rather than the pre-payment value
	# still cached on pi from right after pi.submit().
	pi.reload()

	return {
		"purchase_invoice": pi.name,
		"payment_entry": pe.name if pe else None,
		"supplier": supplier_doc.name,
		"supplier_name": supplier_doc.supplier_name,
		"grand_total": pi.grand_total,
		"outstanding_amount": pi.outstanding_amount,
		"currency": pi.currency,
		"paid_from_account": paid_from_account,
	}


@frappe.whitelist()
def pay_purchase_invoice(
	purchase_invoice: str,
	amount: float = 0,
	mode_of_payment: str = "",
	reference_no: str = "",
	remarks: str = "",
):
	"""Record a payment against a Purchase Invoice that was created earlier
	without one, or only partially paid — the follow-up to Express Purchase's
	own "Invoice Only"/"Partial Payment" options (process_express_purchase()),
	reachable from this page's Purchase Invoice History via the "Pay" button
	on any row still showing an outstanding balance. amount defaults to the
	invoice's full remaining outstanding amount when left at 0/blank; anything
	less is a further partial payment — same paid_amount/received_amount/
	allocated_amount handling as process_express_purchase()'s own
	partial-payment path (see the comment there for why all three need
	setting together). Unlike process_express_purchase(), posting_date isn't a
	form field here — this is a distinct, later payment event, so it's always
	dated today (get_payment_entry()'s own default), not backdated to match
	whatever date the original invoice/purchase happened to carry. Mirrors
	sales.py's pay_sales_invoice().
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	pi = frappe.get_doc("Purchase Invoice", purchase_invoice)
	if pi.docstatus != 1:
		frappe.throw(_("{0} is not a submitted Purchase Invoice.").format(purchase_invoice))

	company = get_selected_company()
	if company and pi.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(purchase_invoice, company))

	if pi.outstanding_amount <= 0:
		frappe.throw(_("{0} has no outstanding balance.").format(purchase_invoice))

	amount = flt(amount) or pi.outstanding_amount
	if amount <= 0:
		frappe.throw(_("Enter an amount greater than zero."))
	if amount > pi.outstanding_amount:
		frappe.throw(
			_("The payment amount can't be more than the outstanding balance ({0}).").format(
				frappe.utils.fmt_money(pi.outstanding_amount, currency=pi.currency)
			)
		)

	if mode_of_payment and not frappe.db.exists(
		"Mode of Payment", {"name": mode_of_payment, "enabled": 1}
	):
		frappe.throw(_("{0} is not an active Mode of Payment.").format(mode_of_payment))

	if not company:
		frappe.throw(_("No company is selected."))

	paid_from_account = _resolve_paid_from_account(company, mode_of_payment)
	if not paid_from_account:
		frappe.throw(
			_(
				"Could not resolve an account to pay from for {0}. Set a default bank account for"
				" the company, or configure one for the chosen Mode of Payment."
			).format(company)
		)

	try:
		pe = get_payment_entry("Purchase Invoice", pi.name, bank_account=paid_from_account)

		# get_payment_entry() defaults paid_amount (and the one reference row's
		# allocated_amount) to the invoice's full outstanding amount — only
		# overridden when paying less than that, same reasoning as
		# process_express_purchase()'s own partial-payment path.
		if amount < pi.outstanding_amount:
			pe.paid_amount = amount
			pe.received_amount = amount
			pe.references[0].allocated_amount = amount

		if mode_of_payment:
			pe.mode_of_payment = mode_of_payment

		pe.reference_no = reference_no.strip() if reference_no and reference_no.strip() else pi.name
		pe.reference_date = nowdate()

		if remarks and remarks.strip():
			pe.remarks = remarks.strip()
			pe.custom_remarks = 1
			pe.custom_line_descriptions = remarks.strip()

		pe.insert()
		pe.submit()

		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Purchase pay_purchase_invoice failed")
		raise

	pi.reload()

	return {
		"purchase_invoice": pi.name,
		"payment_entry": pe.name,
		"grand_total": pi.grand_total,
		"outstanding_amount": pi.outstanding_amount,
		"currency": pi.currency,
		"paid_from_account": paid_from_account,
	}


@frappe.whitelist()
def cancel_express_purchase(purchase_invoice: str):
	"""Cancel a purchase made through this page in one step: every submitted
	Payment Entry against it first, then the Purchase Invoice itself. Order
	matters — a Payment Entry references the invoice's outstanding amount, so
	ERPNext requires it cancelled before the invoice can be; doing both here
	means a purchase is never left half-cancelled (payment reversed but
	invoice still open, or vice versa) the way cancelling each separately in
	Desk risks if the admin stops halfway. Mirrors sales.py's
	cancel_express_sale().
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	pi = frappe.get_doc("Purchase Invoice", purchase_invoice)
	if pi.docstatus != 1:
		frappe.throw(_("{0} is not a submitted Purchase Invoice.").format(purchase_invoice))

	company = get_selected_company()
	if company and pi.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(purchase_invoice, company))

	payment_entries = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Purchase Invoice", "reference_name": purchase_invoice, "docstatus": 1},
		pluck="parent",
	)

	try:
		for pe_name in payment_entries:
			pe = frappe.get_doc("Payment Entry", pe_name)
			if pe.docstatus == 1:
				pe.cancel()

		# Cancelling the Payment Entry(s) above updates the invoice's own
		# outstanding_amount/modified timestamp — reload before cancelling it,
		# or this throws a stale-timestamp TimestampMismatchError.
		pi.reload()
		pi.cancel()

		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Purchase cancel failed")
		raise

	return {"purchase_invoice": pi.name, "cancelled_payment_entries": payment_entries}

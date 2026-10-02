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

# Same fallback ERPNext's own Payment Entry resolution falls back to when no
# Mode of Payment account is configured: the Company's own default bank
# account, or the single Bank-type Account for that company if there's
# exactly one.
from erpnext.accounts.doctype.journal_entry.journal_entry import get_default_bank_cash_account

from ez_accounting.api import (
	apply_permitted_filter,
	ensure_admin,
	get_print_format_settings,
	get_selected_company,
	is_company_restricted,
	upsert_party_default,
)
from ez_accounting.www.forms.user.customer import company_customer_names
from ez_accounting.www.forms.user.items import company_item_names

no_cache = 1


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "sales"
	context.title = _("Express Sales")
	context.company = get_selected_company()
	context.payment_terms_templates = frappe.get_all(
		"Payment Terms Template", pluck="name", order_by="name asc"
	)

	# ?amend=<cancelled Sales Invoice> — the Amend button on Sales Invoice
	# History cancels the sale, then sends the admin back here to edit its
	# details and re-submit as a proper amendment (new invoice named
	# "<original>-1", amended_from linking back). Only a real, cancelled,
	# not-yet-amended invoice for the selected company is accepted; anything
	# else just loads the normal blank form.
	context.amend_from = ""
	amend_from = frappe.form_dict.get("amend")
	if amend_from and _is_amendable_source(amend_from, context.company):
		context.amend_from = amend_from
	return context


def _is_amendable_source(name: str, company: str | None) -> bool:
	si = frappe.db.get_value(
		"Sales Invoice", name, ["docstatus", "company"], as_dict=True
	)
	if not si or si.docstatus != 2:
		return False
	if company and si.company != company:
		return False
	# Frappe allows only one amendment per cancelled document — if this one is
	# already amended, there's nothing to resume here.
	if frappe.db.exists("Sales Invoice", {"amended_from": name}):
		return False
	return True


@frappe.whitelist()
def get_customers():
	"""Customers for the form's customer picker and the history filter.

	Scoped to the selected company the same way /forms/user/customer's list is
	— company_customer_names(): any Sales Invoice / Payment Entry / Quotation
	for that company, plus customers whose custom_company is the selected
	company (so a newly-created customer is pickable before its first invoice).
	A customer with real transactions in several companies remains available
	in each of those companies. No selected company → every permitted customer.
	"""
	ensure_admin()
	# is_student flags Customers in the "Student" group — the form shows a
	# link to /forms/user/student_customer_profile for them (same group the
	# Student Customers pages scope by, see ez_accounting.www.forms.user.student_customer).
	rows = frappe.get_all(
		"Customer",
		filters=apply_permitted_filter({"disabled": 0}, "Customer"),
		fields=["name", "customer_name", "customer_group"],
		order_by="customer_name asc",
	)

	company = get_selected_company()
	if company:
		assigned = set(
			frappe.get_all("Customer", filters={"custom_company": company}, pluck="name")
		)
		allowed = company_customer_names(company) | assigned
		rows = [r for r in rows if r["name"] in allowed]

	for row in rows:
		row["is_student"] = row.pop("customer_group", None) == "Student"
	return rows


@frappe.whitelist()
def get_sales_items():
	"""Active, sellable Items only — standard_rate is included so the client can
	prefill the Rate field on selection, the way Express Payroll prefills a
	remembered amount per employee.

	Scoped to the selected company the same way /forms/user/items' list is
	(company_item_names — items with an Item Default row for that company; new
	items created from that page get one for the current company on creation).
	No selected company → every permitted sellable item, unchanged.
	"""
	ensure_admin()
	filters = apply_permitted_filter([["disabled", "=", 0], ["is_sales_item", "=", 1]], "Item")

	company = get_selected_company()
	if company:
		names = company_item_names(company)
		if not names:
			return []
		filters.append(["name", "in", list(names)])

	return frappe.get_all(
		"Item",
		filters=filters,
		fields=["name", "item_name", "stock_uom", "standard_rate"],
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


def _resolve_paid_into_account(company: str, mode_of_payment: str = "") -> str | None:
	"""Which GL account a sale's payment actually gets received into: the Mode
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
def get_pay_to_preview(mode_of_payment: str = ""):
	"""Preview endpoint for the form: resolves (without touching any document)
	which account a sale would actually be received into right now, so the
	admin can see it before submitting — same resolution process_express_sale()
	itself uses, via _resolve_paid_into_account().
	"""
	ensure_admin()

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	return {"account": _resolve_paid_into_account(company, mode_of_payment)}


@frappe.whitelist()
def get_income_account_preview(customer: str, item: str):
	"""Preview endpoint for the form: given a customer + item, resolve and
	return the Income Account a real sale would post to — via
	set_missing_values() on an unsaved, never-inserted Sales Invoice, the same
	resolution submit itself uses (Item Default -> Item Group Default ->
	Company default), so the preview can never drift from what actually gets
	posted. This is the selling-side counterpart of Express Purchase's
	get_expense_account_preview() — Sales Invoice items resolve an Income
	Account, not an Expense Account, so that's what's previewed here. Customer
	is required even though income-account resolution itself doesn't depend on
	it — set_missing_values() also resolves the party's currency/exchange rate
	and throws without one.
	"""
	ensure_admin()

	if not customer or not item:
		frappe.throw(_("Customer and Item are required."))

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	si = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"customer": customer,
			"company": company,
			"posting_date": nowdate(),
			"items": [{"item_code": item, "qty": 1, "rate": 0}],
		}
	)
	si.set_missing_values(for_validate=True)

	return {"income_account": si.items[0].income_account}


@frappe.whitelist()
def check_unique_id(unique_id: str = ""):
	"""Live availability check for the optional Unique ID field, so the form
	can warn — with a link to the existing invoice — before the admin even
	tries to submit. process_express_sale() re-checks the same way as a
	safety net (e.g. two tabs submitting the same ID at once), so this
	endpoint existing doesn't relax that check, just gives the client a
	chance to catch it earlier.
	"""
	ensure_admin()

	unique_id = (unique_id or "").strip()
	if not unique_id:
		return {"exists": False}

	# A cancelled invoice never really "holds" a Unique ID — excluded here so
	# amending a sale (which cancels the original, keeping its Unique ID) can
	# re-submit with the same ID.
	existing = frappe.db.get_value(
		"Sales Invoice", {"custom_unique_id": unique_id, "docstatus": ["!=", 2]}, "name"
	)
	return {"exists": bool(existing), "sales_invoice": existing}


def _ensure_unique_id_available(unique_id: str):
	existing = frappe.db.get_value(
		"Sales Invoice", {"custom_unique_id": unique_id, "docstatus": ["!=", 2]}, "name"
	)
	if existing:
		frappe.throw(_('Unique ID "{0}" is already used by Sales Invoice {1}.').format(unique_id, existing))


@frappe.whitelist()
def get_sales_invoice_history(
	from_date: str = "",
	to_date: str = "",
	created_from: str = "",
	created_to: str = "",
	due_from: str = "",
	due_to: str = "",
	payment_terms_template: str = "",
	customer_search: str = "",
	item_search: str = "",
	doc_no_search: str = "",
	sort_by: str = "",
	page: int = 1,
	page_size: int = 20,
	show_cancelled: int = 0,
):
	"""Recent Sales Invoices (submitted, plus cancelled ones once show_cancelled
	is truthy — drafts don't belong here regardless, this page never leaves
	one behind) so the admin can see what's already been sold before starting
	a new one, without leaving this page. Each row also
	carries the submitted Payment Entry receiving it (if any) and that entry's
	Mode of Payment — Express Sales always receives payment in full
	immediately, so its own invoices will have exactly one Payment Entry;
	older invoices from outside this page may have none or several (only the
	first is shown). Two independent date ranges narrow the list, each end
	optional: from_date/to_date on posting_date (the business date, which the
	admin can backdate), and created_from/created_to on creation (when the
	record actually got entered). creation is also returned on every row. due_from/due_to
	narrow on due_date the same way.

	customer_search/item_search/doc_no_search (the last a substring of the
	invoice's own document number, e.g. "ACC-SINV-2026-00215") optionally
	narrow further — server-side (not just filtering whatever's already loaded
	client-side), same as the date range.

	sort_by controls row order (the client remembers the choice in
	localStorage): "" / "posting_date_desc" = the default business-date order,
	"created_desc" / "created_asc" = by when the record was actually entered.
	page/page_size provide server-side pagination. Only the page sizes offered
	by the UI are accepted so a crafted request cannot accidentally fetch an
	unbounded history result.

	show_cancelled defaults to folding cancelled invoices out of the list
	entirely (not just visually hiding them client-side) so pagination/totals
	stay correct either way -- the "Show cancelled" checkbox toggles it.
	"""
	ensure_admin()
	try:
		page = max(int(page), 1)
	except (TypeError, ValueError):
		page = 1
	try:
		page_size = int(page_size)
	except (TypeError, ValueError):
		page_size = 20
	if page_size not in {20, 100, 500, 5000}:
		page_size = 20

	def empty_result():
		return {"rows": [], "total": 0, "page": 1, "page_size": page_size, "total_pages": 0}

	ORDER_BY_BY_SORT = {
		"": "posting_date desc, creation desc",
		"posting_date_desc": "posting_date desc, creation desc",
		"created_desc": "creation desc",
		"created_asc": "creation asc",
		"due_asc": "due_date asc, creation desc",
		"due_desc": "due_date desc, creation desc",
	}
	order_by = ORDER_BY_BY_SORT.get(sort_by, ORDER_BY_BY_SORT[""])
	filters = [["docstatus", "in", [1, 2] if int(show_cancelled or 0) else [1]]]
	company = get_selected_company()
	if company:
		filters.append(["company", "=", company])
	elif is_company_restricted():
		# User is limited to Companies (User Permission) but none exist on this
		# site — show nothing rather than falling through to an unscoped list.
		return empty_result()
	if from_date:
		filters.append(["posting_date", ">=", from_date])
	if to_date:
		filters.append(["posting_date", "<=", to_date])
	if created_from:
		filters.append(["creation", ">=", created_from])
	if created_to:
		# creation is a datetime — include the whole of the chosen end day,
		# not just its 00:00:00.
		filters.append(["creation", "<=", f"{created_to} 23:59:59"])
	if due_from:
		filters.append(["due_date", ">=", due_from])
	if due_to:
		filters.append(["due_date", "<=", due_to])
	if payment_terms_template:
		filters.append(["payment_terms_template", "=", payment_terms_template])
	if doc_no_search:
		filters.append(["name", "like", f"%{doc_no_search}%"])
	if item_search:
		# Sales Invoice Item is a child table — resolve to the set of parent
		# invoice names matching first, same "narrow via a name IN (...)
		# subquery" shape used for the batched lookups below, then let the
		# outer query's own date/company filters and ordering/limit apply on
		# top of that set exactly as they would without this filter at all.
		matching_invoices = frappe.get_all(
			"Sales Invoice Item",
			or_filters=[
				["item_name", "like", f"%{item_search}%"],
				["item_code", "like", f"%{item_search}%"],
			],
			pluck="parent",
			distinct=True,
		)
		if not matching_invoices:
			return empty_result()
		filters.append(["name", "in", matching_invoices])

	or_filters = None
	if customer_search:
		or_filters = [
			["customer_name", "like", f"%{customer_search}%"],
			["customer", "like", f"%{customer_search}%"],
		]

	count_result = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		or_filters=or_filters,
		fields=[{"COUNT": "name", "as": "total"}],
	)
	total = int(count_result[0].total or 0) if count_result else 0
	if not total:
		return empty_result()
	total_pages = (total + page_size - 1) // page_size
	page = min(page, total_pages)

	invoices = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"customer",
			"customer_name",
			"posting_date",
			"due_date",
			"payment_terms_template",
			"creation",
			"owner",
			"grand_total",
			"outstanding_amount",
			"currency",
			"status",
		],
		order_by=order_by,
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	if not invoices:
		return empty_result()

	payment_entries = frappe.get_all(
		"Payment Entry Reference",
		filters={
			"reference_doctype": "Sales Invoice",
			"reference_name": ["in", [inv.name for inv in invoices]],
			"docstatus": 1,
		},
		fields=["reference_name", "parent", "allocated_amount"],
	)
	payment_entry_by_invoice = {}
	payment_entries_by_invoice = {}
	# Counted (not just the first-found parent above) so the client can tell
	# a normal one-Payment-Entry sale apart from a Split Payment one (more
	# than one submitted Payment Entry created together by
	# process_express_sale()'s "split" mode) — a split sale's "Receipt" link
	# needs to point at a different, invoice-level print format that sums
	# all of them, since no single Payment Entry holds the full amount paid.
	payment_entry_count_by_invoice = {}
	for ref in payment_entries:
		payment_entry_by_invoice.setdefault(ref.reference_name, ref.parent)
		payment_entries_by_invoice.setdefault(ref.reference_name, []).append(ref)
		payment_entry_count_by_invoice[ref.reference_name] = (
			payment_entry_count_by_invoice.get(ref.reference_name, 0) + 1
		)

	# The receipt ID shown in the History table's own ID column is a
	# separate concern from payment_entry/payment_entry_count above (which
	# only ever look at *submitted* references, since those two drive the
	# "Receipt" print link/format — printing a voided receipt makes no
	# sense). A Cancelled Sales Invoice's own Payment Entry was cancelled
	# right alongside it (see cancel_express_sale()), so its reference row's
	# docstatus is 2, not 1 — without this, every cancelled sale's ID column
	# shows a bare dash even though a real (now-voided) receipt did exist,
	# which is exactly what an admin scanning this table for "what receipt
	# was this" needs to see, just clearly marked as voided.
	receipt_pe_by_invoice = dict(payment_entry_by_invoice)
	receipt_pe_count_by_invoice = dict(payment_entry_count_by_invoice)
	receipt_pe_voided = set()
	need_cancelled = [inv.name for inv in invoices if inv.name not in receipt_pe_by_invoice]
	if need_cancelled:
		cancelled_refs = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_doctype": "Sales Invoice", "reference_name": ["in", need_cancelled], "docstatus": 2},
			fields=["reference_name", "parent"],
			order_by="modified desc",
		)
		for ref in cancelled_refs:
			receipt_pe_by_invoice.setdefault(ref.reference_name, ref.parent)
			receipt_pe_count_by_invoice[ref.reference_name] = (
				receipt_pe_count_by_invoice.get(ref.reference_name, 0) + 1
			)
			receipt_pe_voided.add(ref.reference_name)

	# paid_to/mode_of_payment on the Payment Entry itself are the actual
	# account and mode the sale was received through — same account
	# _resolve_paid_into_account() resolves before payment, just read back
	# off the real submitted record here rather than re-resolved (an
	# invoice's payment could in principle have been receipted outside this
	# page, into a different account/mode).
	pe_by_name = {}
	all_payment_names = {ref.parent for ref in payment_entries} | {
		name for name in receipt_pe_by_invoice.values() if name
	}
	if all_payment_names:
		pe_rows = frappe.get_all(
			"Payment Entry",
			filters={"name": ["in", sorted(all_payment_names)]},
			fields=["name", "posting_date", "paid_to", "mode_of_payment", "custom_deposit_sheet", "custom_sequence_number"],
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

	# A cancelled invoice can still be amended from this page (?amend=…) as long
	# as it isn't already amended — Frappe allows only one amendment per
	# cancelled doc. One batch query for every cancelled row on screen.
	cancelled_names = [inv.name for inv in invoices if inv.status == "Cancelled"]
	already_amended = set()
	if cancelled_names:
		already_amended = set(
			frappe.get_all(
				"Sales Invoice",
				filters={"amended_from": ["in", cancelled_names]},
				pluck="amended_from",
			)
		)

	# Which of these invoices' customers are students (Customer Group
	# "Student") — the row then links to that student's profile. One batch
	# query over the distinct customers on screen.
	customers = {inv.customer for inv in invoices if inv.customer}
	student_customers = set()
	if customers:
		student_customers = set(
			frappe.get_all(
				"Customer",
				filters={"name": ["in", list(customers)], "customer_group": "Student"},
				pluck="name",
			)
		)

	# One customer-level balance/action marker for the history table. The
	# button belongs only on that customer's latest submitted invoice, rather
	# than being repeated on every historical row. SUM(outstanding_amount)
	# naturally nets credit notes (negative outstanding) against invoices.
	latest_invoice_by_customer = {}
	net_balance_by_customer = {}
	if customers:
		balance_conditions = ["docstatus = 1", "customer IN %(customers)s"]
		balance_values = {"customers": tuple(customers)}
		if company:
			balance_conditions.append("company = %(company)s")
			balance_values["company"] = company
		for balance_row in frappe.db.sql(
			f"""
			SELECT customer,
				COALESCE(SUM(outstanding_amount), 0) AS net_balance,
				SUBSTRING_INDEX(
					GROUP_CONCAT(name ORDER BY posting_date DESC, creation DESC), ',', 1
				) AS last_invoice
			FROM `tabSales Invoice`
			WHERE {' AND '.join(balance_conditions)}
			GROUP BY customer
			""",
			balance_values,
			as_dict=True,
		):
			net_balance_by_customer[balance_row.customer] = flt(balance_row.net_balance)
			latest_invoice_by_customer[balance_row.customer] = balance_row.last_invoice

	for inv in invoices:
		pe_name = payment_entry_by_invoice.get(inv.name)
		pe_row = pe_by_name.get(pe_name)
		inv["payment_entry"] = pe_name
		inv["payment_entry_count"] = payment_entry_count_by_invoice.get(inv.name, 0)
		# For the ID column's own "Receipt" sub-line only -- may be a voided
		# (cancelled) Payment Entry when the sale itself is Cancelled (see the
		# comment above receipt_pe_by_invoice's own definition).
		inv["receipt_payment_entry"] = receipt_pe_by_invoice.get(inv.name)
		inv["receipt_payment_entry_count"] = receipt_pe_count_by_invoice.get(inv.name, 0)
		inv["receipt_payment_entry_voided"] = inv.name in receipt_pe_voided
		sequence_ids = [
			pe_by_name[ref.parent].custom_sequence_number
			for ref in payment_entries_by_invoice.get(inv.name, [])
			if pe_by_name.get(ref.parent) and pe_by_name[ref.parent].custom_sequence_number
		]
		if not sequence_ids and inv["receipt_payment_entry"]:
			receipt_pe = pe_by_name.get(inv["receipt_payment_entry"])
			if receipt_pe and receipt_pe.custom_sequence_number:
				sequence_ids.append(receipt_pe.custom_sequence_number)
		inv["sequence_id"] = ", ".join(dict.fromkeys(sequence_ids))
		inv["pay_to"] = pe_row.paid_to if pe_row else None
		inv["mode_of_payment"] = pe_row.mode_of_payment if pe_row else None
		# Posting date of the receiving Payment Entry — the "Payment" line in the
		# history table's combined Dates column. None when the sale has no
		# submitted Payment Entry (Invoice Only, or still unpaid).
		inv["payment_date"] = str(pe_row.posting_date) if pe_row and pe_row.posting_date else None
		# The Forms Bank Deposit Sheet that has deposited this receipt, if any —
		# set by that sheet on submit, cleared on cancel (see
		# [[forms-bank-deposit-sheet]]). Sum the allocation from every deposited
		# Payment Entry, rather than only inspecting the first receipt, so split
		# and partial payments report the amount actually deposited for this invoice.
		invoice_refs = payment_entries_by_invoice.get(inv.name, [])
		deposited_refs = [
			ref for ref in invoice_refs
			if pe_by_name.get(ref.parent) and pe_by_name[ref.parent].custom_deposit_sheet
		]
		deposit_sheets = list(dict.fromkeys(pe_by_name[ref.parent].custom_deposit_sheet for ref in deposited_refs))
		inv["deposit_sheet"] = deposit_sheets[0] if deposit_sheets else None
		inv["deposit_sheets"] = deposit_sheets
		inv["deposited_amount"] = sum(flt(ref.allocated_amount) for ref in deposited_refs)
		# "Deposited / Not deposited" only makes sense for a receipt that landed
		# in an Undeposited-Funds staging account — a payment received straight
		# into a bank/cash account never goes through a deposit sheet, so its
		# cell shows a plain dash instead of a misleading "Not deposited".
		# Keyed off the Mode of Payment name or the Payment Entry's paid-to
		# account containing "undeposited".
		haystack = f"{pe_row.mode_of_payment or ''} {pe_row.paid_to or ''}".lower() if pe_row else ""
		inv["awaiting_deposit"] = bool(
			pe_name and not inv["deposit_sheet"] and "undeposited" in haystack
		)
		inv["created_by"] = full_name_by_owner.get(inv.owner) or inv.owner
		inv["amendable"] = inv.status == "Cancelled" and inv.name not in already_amended
		inv["is_student"] = inv.customer in student_customers
		inv["customer_net_balance"] = net_balance_by_customer.get(inv.customer, 0)
		inv["last_invoice"] = latest_invoice_by_customer.get(inv.customer)
		inv["is_last_invoice"] = inv.name == inv["last_invoice"]

	return {
		"rows": invoices,
		"total": total,
		"page": page,
		"page_size": page_size,
		"total_pages": total_pages,
	}


@frappe.whitelist()
def get_sales_invoice_payment_history(sales_invoice: str):
	"""Every Payment Entry ever recorded against this Sales Invoice — the full
	trail, not just the single one get_sales_invoice_history() surfaces for
	its own Receipt/Pay-To/Mode-of-Payment columns (that function only ever
	picks the first submitted Payment Entry it finds, which was a reasonable
	simplification back when Express Sales only ever produced exactly one).
	Now that a sale can be paid off across more than one Payment Entry
	(Partial Payment at creation, followed by one or more later payments via
	the "Pay" button), the admin needs the whole picture — including any
	Payment Entry that was later cancelled, which the main table's "first
	submitted" pick would otherwise hide entirely. allocated_amount (not the
	Payment Entry's own paid_amount) is what's shown per row — the amount
	that Payment Entry actually applied to *this* invoice, in case it's ever
	one that also paid off other invoices at the same time. `unallocated_amount`
	is the whole Payment Entry's surplus (an overpayment kept as the customer's
	on-account advance — see [[forms-express-sales]]) — a PE property, not
	per-invoice, so a receipt that overpaid this invoice shows its full
	unallocated figure here.
	"""
	ensure_admin()

	invoice_company = frappe.db.get_value("Sales Invoice", sales_invoice, "company")
	if not invoice_company:
		frappe.throw(_("{0} is not a valid Sales Invoice.").format(sales_invoice))

	company = get_selected_company()
	if company and invoice_company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(sales_invoice, company))

	references = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": sales_invoice},
		fields=["parent", "allocated_amount"],
	)
	if not references:
		return []

	allocated_by_pe = {row.parent: row.allocated_amount for row in references}

	pe_rows = frappe.get_all(
		"Payment Entry",
		filters={"name": ["in", list(allocated_by_pe.keys())]},
		fields=[
			"name",
			"posting_date",
			"mode_of_payment",
			"reference_no",
			"docstatus",
			"custom_deposit_sheet",
			"paid_amount",
			"unallocated_amount",
		],
		order_by="posting_date asc, creation asc",
	)

	status_label = {0: _("Draft"), 1: _("Submitted"), 2: _("Cancelled")}
	for row in pe_rows:
		row["allocated_amount"] = allocated_by_pe.get(row.name)
		row["status"] = status_label.get(row.docstatus, "")
		row["deposit_sheet"] = row.pop("custom_deposit_sheet", None)

	return pe_rows


def _parse_line_items(items: str | list) -> list[dict]:
	"""Parse+validate the Item/Qty/Rate rows for a multi-line sale. Each row
	is checked the same way the old single-item form checked its one row —
	item must be an active sales Item, qty/rate both > 0 — just looped over
	every row now instead of asserting it once. Returns rows with item_doc
	resolved and qty/rate coerced to float, in the order submitted (so the
	created Sales Invoice's line order matches what the admin saw on screen).

	`rate` is always the positive magnitude the admin typed — a "Deduction"
	row (is_deduction=True, e.g. a scholarship/discount Item) negates it here
	server-side rather than trusting a client-computed signed value, same
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
			"Item", item_code, ["name", "item_name", "disabled", "is_sales_item"], as_dict=True
		)
		if not item_doc or item_doc.disabled or not item_doc.is_sales_item:
			frappe.throw(_("{0} is not an active sales item.").format(item_code))

		parsed.append(
			{
				"item_doc": item_doc,
				"qty": qty,
				"rate": -rate if is_deduction else rate,
				"description": description or None,
			}
		)

	return parsed


def _parse_payment_lines(payment_lines: str | list) -> list[dict]:
	"""Parse+validate the Mode of Payment/Amount rows for a "Split Payment"
	sale — e.g. a $650 invoice (170 registration + 480 materials) paid as
	$300 cash + $350 credit card in one run. mode_of_payment is optional per
	row, same as the single mode_of_payment field on Full/Partial Payment —
	left blank, that row's account resolves to the company's own default
	bank account (see _resolve_paid_into_account()). Doesn't check the rows'
	total against the invoice here — process_express_sale() does that once
	it knows the real outstanding_amount, the same way "partial" validates
	partial_amount against si.outstanding_amount rather than here.
	"""
	if isinstance(payment_lines, str):
		try:
			payment_lines = json.loads(payment_lines)
		except (TypeError, ValueError):
			frappe.throw(_("Invalid payment methods payload."))

	if not payment_lines or not isinstance(payment_lines, list):
		frappe.throw(_("At least one payment method is required for a split payment."))

	parsed = []
	for row in payment_lines:
		mode_of_payment = (row.get("mode_of_payment") or "").strip()
		amount = flt(row.get("amount"))
		if amount <= 0:
			frappe.throw(_("Amount must be greater than zero for every payment method."))
		parsed.append({"mode_of_payment": mode_of_payment, "amount": amount})

	return parsed


@frappe.whitelist()
def process_express_sale(
	customer: str,
	items: str | list,
	posting_date: str = "",
	payment_mode: str = "full",
	partial_amount: float = 0,
	mode_of_payment: str = "",
	payment_lines: str | list = None,
	reference_no: str = "",
	remarks: str = "",
	unique_id: str = "",
	amended_from: str = "",
	payment_terms_template: str = "",
	due_date: str = "",
):
	"""Run a sale end-to-end: Sales Invoice with one or more lines, submitted,
	then — unless payment_mode is "invoice_only" — one or more matching
	Payment Entries (via ERPNext's own get_payment_entry) recording that it
	was received, also submitted. `items` is a JSON list of {item, qty, rate,
	description, is_deduction} rows (description optional, same fallback as
	before this field existed; is_deduction optional, negates that row's
	rate — see _parse_line_items()) — get_payment_entry() needs no changes at
	all for multiple lines, since it only ever reads the invoice's own
	outstanding_amount regardless of how many rows produced it.

	payment_mode is one of:
	  - "full" (default): pay the invoice's full outstanding amount now,
		exactly the original single-mode behavior.
	  - "partial": pay only partial_amount now (must be > 0 and <= the
		invoice's outstanding amount) — the rest stays outstanding on the
		invoice for a later payment.
	  - "split": pay the invoice's full outstanding amount now, same as
		"full", but split across more than one Mode of Payment/Amount pair
		(payment_lines, a JSON list of {mode_of_payment, amount} — see
		_parse_payment_lines()) — e.g. a $650 sale paid as $300 cash + $350
		credit card. One Payment Entry gets created per row, each hitting
		its own resolved account, so a parent paying with two different
		methods is still one invoice/one submission on this page, not two
		separate sales. The rows must add up to exactly the invoice total —
		unlike "partial", a split payment can't leave a balance outstanding.
	  - "invoice_only": create and submit the Sales Invoice only, no Payment
		Entry at all — for recording a sale made on credit / to be paid
		later. mode_of_payment/reference_no are meaningless without a
		payment and are ignored in this mode (remarks still reaches the
		invoice either way).

	posting_date defaults to today when left blank — due_date and the Payment
	Entry's reference date both follow it, since this whole flow represents
	one same-day sale, just not always literally today's date (e.g. entering
	a sale a day late). Which account payment is received into is resolved by
	_resolve_paid_into_account() — see there; there's no explicit Bank Account
	field on this form, the resolved account is only shown as a preview
	(get_pay_to_preview()) before submitting. reference_no/remarks fall back
	to sensible defaults (see below) when left blank. unique_id is entirely
	optional (blank is always allowed, any number of invoices can be blank),
	but when given must not already be used by another Sales Invoice — checked
	up front, before anything is created, via _ensure_unique_id_available()
	(the same check check_unique_id() exposes for the form's own live
	before-submit warning).
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	if payment_mode not in ("full", "partial", "invoice_only", "split"):
		frappe.throw(_("Invalid payment mode."))

	unique_id = (unique_id or "").strip()
	if unique_id:
		_ensure_unique_id_available(unique_id)

	# amended_from: set only by the Amend flow (see amend_express_sale / the
	# ?amend= form mode). The new invoice becomes a real Frappe amendment of
	# the cancelled original — named "<original>-1", linked back via
	# amended_from — rather than an unrelated new sale.
	amended_from = (amended_from or "").strip()
	if amended_from:
		source = frappe.db.get_value(
			"Sales Invoice", amended_from, ["docstatus", "company"], as_dict=True
		)
		if not source or source.docstatus != 2:
			frappe.throw(_("{0} is not a cancelled Sales Invoice to amend.").format(amended_from))

	line_items = _parse_line_items(items)

	posting_date = getdate(posting_date) if posting_date else getdate(nowdate())

	customer_doc = frappe.db.get_value(
		"Customer", customer, ["name", "customer_name", "disabled"], as_dict=True
	)
	if not customer_doc or customer_doc.disabled:
		frappe.throw(_("{0} is not an active customer.").format(customer))

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	# Mode of Payment/Pay-To resolution only matters when a payment is
	# actually going to be recorded this run — skipped entirely for
	# "invoice_only" (no payment at all) and "split" (resolved per row
	# below instead of once here) so a misconfigured (or simply blank) Mode
	# of Payment never blocks recording a sale that either never touches a
	# bank account, or resolves its account(s) another way.
	paid_into_account = None
	if payment_mode not in ("invoice_only", "split"):
		if mode_of_payment and not frappe.db.exists(
			"Mode of Payment", {"name": mode_of_payment, "enabled": 1}
		):
			frappe.throw(_("{0} is not an active Mode of Payment.").format(mode_of_payment))

		paid_into_account = _resolve_paid_into_account(company, mode_of_payment)
		if not paid_into_account:
			frappe.throw(
				_(
					"Could not resolve an account to receive payment into for {0}. Set a default bank"
					" account for the company, or configure one for the chosen Mode of Payment."
				).format(company)
			)

	# Split payment's rows are validated/resolved up front too — same
	# fail-fast-before-creating-anything shape as the single-mode block
	# above, just once per row instead of once overall.
	split_lines = None
	if payment_mode == "split":
		split_lines = _parse_payment_lines(payment_lines)
		for line in split_lines:
			if line["mode_of_payment"] and not frappe.db.exists(
				"Mode of Payment", {"name": line["mode_of_payment"], "enabled": 1}
			):
				frappe.throw(_("{0} is not an active Mode of Payment.").format(line["mode_of_payment"]))

			line["paid_into_account"] = _resolve_paid_into_account(company, line["mode_of_payment"])
			if not line["paid_into_account"]:
				frappe.throw(
					_(
						"Could not resolve an account to receive payment into for {0}. Set a default"
						" bank account for the company, or configure one for the chosen Mode of"
						" Payment."
					).format(company)
				)

	# A manually entered Due Date wins over Terms (ERPNext would otherwise
	# rebuild due_date from the template on validate), so Terms is dropped.
	due_date = (due_date or "").strip()
	if due_date:
		if getdate(due_date) < getdate(posting_date):
			frappe.throw(_("Due Date cannot be before Posting Date."))
		payment_terms_template = ""

	try:
		# Step 1 — Sales Invoice for the lines entered. Income account, cost
		# center, and receivable account are all left for ERPNext's own
		# SellingController to resolve from Item/Company defaults on
		# insert/submit, rather than looked up and set by hand here.
		si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": customer_doc.name,
				"company": company,
				"amended_from": amended_from or None,
				"posting_date": posting_date,
				# Without this, Frappe silently forces posting_date back to today
				# regardless of what's set above — leaving due_date (also set to
				# posting_date, below) stuck in the past and failing "Due Date
				# cannot be before Posting Date".
				"set_posting_time": 1,
				"due_date": due_date or posting_date,
				# Optional Terms — ERPNext rebuilds the payment schedule and
				# due_date from the template on validate.
				"payment_terms_template": payment_terms_template or None,
				# Populated so the custom remarks the admin types actually reach
				# the invoice itself, not just the Payment Entry — the Invoice
				# print formats ("Sales Invoice Standard") show this in a Remarks
				# section when non-empty.
				"remarks": remarks.strip() if remarks and remarks.strip() else None,
				"custom_unique_id": unique_id or None,
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
		si.insert()
		si.submit()

		pe = None
		payment_entries = []
		if payment_mode == "split":
			# Step 2 (split) — one Payment Entry per {mode_of_payment, amount}
			# row, each against the same invoice, each received into its own
			# resolved account. Rows must add up to exactly the invoice's
			# outstanding amount (checked here, once it's a real number, same
			# reasoning as "partial" checking partial_amount against
			# si.outstanding_amount below rather than against a client-
			# computed total) — a split payment can't leave a balance
			# outstanding, unlike "partial".
			total_lines = flt(sum(line["amount"] for line in split_lines))
			precision = si.precision("outstanding_amount")
			if round(total_lines, precision) != round(flt(si.outstanding_amount), precision):
				frappe.throw(
					_(
						"The payment methods must add up to exactly the invoice total ({0}). They"
						" currently total {1}."
					).format(
						frappe.utils.fmt_money(si.outstanding_amount, currency=si.currency),
						frappe.utils.fmt_money(total_lines, currency=si.currency),
					)
				)

			for line in split_lines:
				line_pe = get_payment_entry("Sales Invoice", si.name, bank_account=line["paid_into_account"])
				line_pe.posting_date = posting_date
				line_pe.paid_amount = line["amount"]
				line_pe.received_amount = line["amount"]
				line_pe.references[0].allocated_amount = line["amount"]
				if line["mode_of_payment"]:
					line_pe.mode_of_payment = line["mode_of_payment"]
				line_pe.reference_no = reference_no.strip() if reference_no and reference_no.strip() else si.name
				line_pe.reference_date = posting_date
				if remarks and remarks.strip():
					line_pe.remarks = remarks.strip()
					line_pe.custom_remarks = 1
					line_pe.custom_line_descriptions = remarks.strip()
				line_pe.insert()
				line_pe.submit()
				payment_entries.append(
					{
						"name": line_pe.name,
						"mode_of_payment": line["mode_of_payment"],
						"amount": line["amount"],
						"paid_into_account": line["paid_into_account"],
					}
				)
		elif payment_mode != "invoice_only":
			# Step 2 — Payment Entry against the invoice just created, received
			# into paid_into_account resolved above. payment_type is resolved
			# by get_payment_entry() itself from the doctype ("Receive" for a
			# Sales Invoice). get_payment_entry() defaults paid_amount (and
			# the one reference row's allocated_amount) to the invoice's full
			# outstanding amount — for "partial", both are overridden below to
			# partial_amount instead, so only that much actually gets applied
			# and the remainder stays outstanding on the invoice for a later
			# payment.
			pe = get_payment_entry("Sales Invoice", si.name, bank_account=paid_into_account)

			# get_payment_entry() hardcodes posting_date to nowdate() regardless
			# of the invoice it's built from — override it to match, so a
			# backdated sale doesn't silently end up with a payment dated
			# today. Left unnoticed, this breaks anything that reconciles by
			# Payment Entry date (e.g. Forms Bank Deposit Sheet's
			# expected-cash/checks calc).
			pe.posting_date = posting_date

			if payment_mode == "partial":
				partial_amount = flt(partial_amount)
				if partial_amount <= 0:
					frappe.throw(_("Enter an amount greater than zero for the partial payment."))
				# partial_amount MAY exceed the invoice total — an overpayment.
				# ERPNext caps the allocation to this invoice at its outstanding
				# amount and books the excess as the customer's Unallocated
				# Amount (an on-account advance, applicable to a future invoice
				# via Get Advances / Payment Reconciliation). See the case note
				# in [[forms-express-sales]].
				#
				# Both paid_amount and received_amount need setting (not just
				# one) — this app is single-currency throughout, but Payment
				# Entry still validates paid_amount >= received_amount even when
				# they're meant to be identical (validate_received_amount()), so
				# leaving received_amount at the full-outstanding value
				# get_payment_entry() set would fail that check.
				pe.paid_amount = partial_amount
				pe.received_amount = partial_amount
				pe.references[0].allocated_amount = min(partial_amount, flt(si.outstanding_amount))

			# Recorded for reference regardless of whether it drove paid_into_account.
			if mode_of_payment:
				pe.mode_of_payment = mode_of_payment

			# Reference No + Reference Date are mandatory whenever either leg
			# of the entry is a Bank-type account (Payment Entry.
			# validate_mandatory(), same constraint Express Payroll hits on
			# its Journal Entry) — default to tying it back to the invoice
			# it's settling when the admin didn't type an actual
			# cheque/reference number.
			pe.reference_no = reference_no.strip() if reference_no and reference_no.strip() else si.name
			pe.reference_date = posting_date

			# Remarks: left to Payment Entry's own auto-generated summary
			# unless the admin typed something — custom_remarks has to be set
			# too, otherwise set_remarks() (called from validate()) silently
			# overwrites whatever's in .remarks with its own auto-generated
			# text. custom_line_descriptions is the field that actually
			# matters for print — "Official Receipt with line descriptions"
			# (this doctype's own default print format) reads THAT field for
			# its per-line remarks text, not .remarks at all; without also
			# setting it, whatever the admin types here never showed up on
			# the printed receipt regardless of what .remarks held.
			if remarks and remarks.strip():
				pe.remarks = remarks.strip()
				pe.custom_remarks = 1
				pe.custom_line_descriptions = remarks.strip()

			pe.insert()
			pe.submit()

		# Remember Item/Mode of Payment/Reference No/Remarks for this customer
		# so the next Express Sale for them prefills — inside the same try so
		# a failure here rolls back with everything else rather than
		# silently succeeding on the sale but not on the memory of it. Forms
		# Party Default only ever remembers one item, so a multi-line sale
		# remembers its first row — still a reasonable starting point for
		# next time, just not a full memory of every line. Rate is
		# deliberately NOT remembered here (unlike Express Purchase) — the
		# selected Item already has its own standard_rate to prefill from, so
		# a stale customer-specific rate would only risk overriding a price
		# that's since changed in the Item master. Passing None also clears
		# any rate a binding previously had from before this changed.
		# mode_of_payment/reference_no are blanked out for "invoice_only" —
		# neither was actually used this run, so remembering them would
		# prefill a payment method for a customer's next sale that has
		# nothing to do with how (or whether) this one got paid. The raw
		# reference_no/remarks parameters are passed, not pe.reference_no/
		# pe.remarks — those may hold this invoice's own auto-generated
		# fallback (see above), which would be a wrong thing to prefill next time.
		# "split" has no single mode_of_payment to remember either (it's N of
		# them) — blanked out the same way "invoice_only" is, rather than
		# picking one row arbitrarily to prefill next time.
		upsert_party_default(
			"Customer",
			customer_doc.name,
			company,
			line_items[0]["item_doc"].name,
			None,
			mode_of_payment if payment_mode not in ("invoice_only", "split") else "",
			(reference_no.strip() if reference_no and reference_no.strip() else None)
			if payment_mode != "invoice_only"
			else None,
			remarks.strip() if remarks and remarks.strip() else None,
		)

		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Sales failed")
		raise

	# pe.submit() (above) updates the invoice's outstanding_amount via a
	# direct db_set on the Sales Invoice row, not through this in-memory si
	# object — reload so the response reflects the real post-payment
	# outstanding balance (0 for "full", the remainder for "partial", the
	# full grand_total for "invoice_only") rather than the pre-payment value
	# still cached on si from right after si.submit().
	si.reload()

	return {
		"sales_invoice": si.name,
		"payment_entry": pe.name if pe else None,
		"payment_entries": payment_entries or None,
		"customer": customer_doc.name,
		"customer_name": customer_doc.customer_name,
		"grand_total": si.grand_total,
		"outstanding_amount": si.outstanding_amount,
		"currency": si.currency,
		"paid_into_account": paid_into_account,
		"unique_id": si.custom_unique_id,
		"amended_from": si.amended_from,
	}


@frappe.whitelist()
def pay_sales_invoice(
	sales_invoice: str,
	amount: float = 0,
	mode_of_payment: str = "",
	reference_no: str = "",
	remarks: str = "",
	posting_date: str = "",
	amended_from: str = "",
):
	"""Record a payment against a Sales Invoice that was created earlier
	without one, or only partially paid — the follow-up to Express Sale's own
	"Invoice Only"/"Partial Payment" options (process_express_sale()),
	reachable from this page's Sales Invoice History via the "Pay" button on
	any row still showing an outstanding balance. amount defaults to the
	invoice's full remaining outstanding amount when left at 0/blank;
	anything less is a further partial payment — same paid_amount/
	received_amount/allocated_amount handling as process_express_sale()'s own
	partial-payment path (see the comment there for why all three need
	setting together). posting_date defaults to today when left blank (same
	default as process_express_sale()'s own field) — this is usually a
	distinct, later payment event, so today is the sensible default, but the
	admin can backdate it (e.g. entering a payment a day late) same as the
	Express Sale form already allows for the original invoice.
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted Sales Invoice.").format(sales_invoice))

	company = get_selected_company()
	if company and si.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(sales_invoice, company))

	if si.outstanding_amount <= 0:
		frappe.throw(_("{0} has no outstanding balance.").format(sales_invoice))

	amount = flt(amount) or si.outstanding_amount
	if amount <= 0:
		frappe.throw(_("Enter an amount greater than zero."))
	# `amount` MAY exceed the outstanding balance — an overpayment. ERPNext
	# caps the allocation to this invoice at its outstanding amount and books
	# the excess as the customer's Unallocated Amount (an on-account advance,
	# applicable to a future invoice via Get Advances / Payment
	# Reconciliation). See the case note in [[forms-express-sales]].

	if mode_of_payment and not frappe.db.exists(
		"Mode of Payment", {"name": mode_of_payment, "enabled": 1}
	):
		frappe.throw(_("{0} is not an active Mode of Payment.").format(mode_of_payment))

	if not company:
		frappe.throw(_("No company is selected."))
	if amended_from:
		source = frappe.get_doc("Payment Entry", amended_from)
		if source.docstatus != 2 or source.company != company or source.party != si.customer:
			frappe.throw(_("{0} is not a cancelled payment for this invoice's customer.").format(amended_from))
		if frappe.db.exists("Payment Entry", {"amended_from": amended_from}):
			frappe.throw(_("{0} has already been amended.").format(amended_from))

	paid_into_account = _resolve_paid_into_account(company, mode_of_payment)
	if not paid_into_account:
		frappe.throw(
			_(
				"Could not resolve an account to receive payment into for {0}. Set a default bank"
				" account for the company, or configure one for the chosen Mode of Payment."
			).format(company)
		)

	posting_date = getdate(posting_date) if posting_date else getdate(nowdate())

	try:
		pe = get_payment_entry("Sales Invoice", si.name, bank_account=paid_into_account)

		# get_payment_entry() hardcodes posting_date to nowdate() regardless —
		# override it the same way process_express_sale() already has to (see
		# its own comment on this exact landmine: left unnoticed, this breaks
		# anything that reconciles by Payment Entry date, e.g. the Bank
		# Deposit Sheet's expected-cash/checks calculation).
		pe.posting_date = posting_date

		# get_payment_entry() defaults paid_amount (and the one reference
		# row's allocated_amount) to the invoice's full outstanding amount.
		# Override whenever the admin paid a different figure — less (a
		# further partial payment) or more (an overpayment; the excess over
		# outstanding lands in Unallocated Amount as a customer advance).
		# allocated_amount is always capped at the outstanding balance.
		if flt(amount) != flt(si.outstanding_amount):
			pe.paid_amount = amount
			pe.received_amount = amount
			pe.references[0].allocated_amount = min(flt(amount), flt(si.outstanding_amount))

		if mode_of_payment:
			pe.mode_of_payment = mode_of_payment

		pe.reference_no = reference_no.strip() if reference_no and reference_no.strip() else si.name
		pe.reference_date = posting_date
		if amended_from:
			pe.amended_from = amended_from

		if remarks and remarks.strip():
			pe.remarks = remarks.strip()
			pe.custom_remarks = 1
			pe.custom_line_descriptions = remarks.strip()

		pe.insert()
		pe.submit()

		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Sales pay_sales_invoice failed")
		raise

	si.reload()

	return {
		"sales_invoice": si.name,
		"payment_entry": pe.name,
		"grand_total": si.grand_total,
		"outstanding_amount": si.outstanding_amount,
		"currency": si.currency,
		"paid_into_account": paid_into_account,
	}


def _ensure_not_deposited(sales_invoice: str, payment_entries: list[str]):
	"""Refuse to cancel `sales_invoice` if any of `payment_entries` has
	already been swept into a Forms Bank Deposit Sheet (see
	[[forms-bank-deposit-sheet]]) — that sheet posted its own real Journal
	Entry against the money; cancelling the sale here would leave it
	referencing a Payment Entry that no longer exists, with the actual cash
	still marked deposited and nothing behind it. Cancelling the deposit
	sheet first (which clears custom_deposit_sheet back off the Payment
	Entry) is the safe order — shared by cancel_express_sale() and
	amend_express_sale(), since amending also cancels the sale first.
	"""
	if not payment_entries:
		return
	deposited = frappe.get_all(
		"Payment Entry",
		filters={"name": ["in", payment_entries]},
		fields=["name", "custom_deposit_sheet"],
	)
	deposit_sheets = sorted({pe.custom_deposit_sheet for pe in deposited if pe.custom_deposit_sheet})
	if deposit_sheets:
		frappe.throw(
			_(
				"{0}'s receipt has already been deposited ({1}). Cancel that deposit sheet first, then cancel this sale."
			).format(sales_invoice, ", ".join(deposit_sheets))
		)


@frappe.whitelist()
def cancel_express_sale(sales_invoice: str):
	"""Cancel a sale made through this page in one step: every submitted
	Payment Entry against it first, then the Sales Invoice itself. Order
	matters — a Payment Entry references the invoice's outstanding amount, so
	ERPNext requires it cancelled before the invoice can be; doing both here
	means a sale is never left half-cancelled (payment reversed but invoice
	still open, or vice versa) the way cancelling each separately in Desk
	risks if the admin stops halfway.
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted Sales Invoice.").format(sales_invoice))

	company = get_selected_company()
	if company and si.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(sales_invoice, company))

	payment_entries = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": sales_invoice, "docstatus": 1},
		pluck="parent",
	)
	_ensure_not_deposited(sales_invoice, payment_entries)

	try:
		for pe_name in payment_entries:
			pe = frappe.get_doc("Payment Entry", pe_name)
			if pe.docstatus == 1:
				pe.cancel()

		# Cancelling the Payment Entry(s) above updates the invoice's own
		# outstanding_amount/modified timestamp — reload before cancelling it,
		# or this throws a stale-timestamp TimestampMismatchError.
		si.reload()
		si.cancel()

		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Sales cancel failed")
		raise

	return {"sales_invoice": si.name, "cancelled_payment_entries": payment_entries}


@frappe.whitelist()
def amend_express_sale(sales_invoice: str):
	"""Start an amendment: cancel the sale (its submitted Payment Entry(s)
	first, then the Sales Invoice itself — the exact order/reasoning as
	cancel_express_sale), and hand the caller back its name so the Express
	Sale form can reopen pre-filled with its details (see get_amend_form_data
	and the ?amend= form mode). The corrected invoice is then created by
	re-submitting that form — process_express_sale() with amended_from set —
	so it becomes a real Frappe amendment ("<original>-1", linked back), not
	an unrelated new sale. Nothing is left half-done: if the admin never
	re-submits, the original is simply a cancelled sale they can amend again.
	"""
	ensure_admin()

	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to run this."), frappe.PermissionError)

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted Sales Invoice.").format(sales_invoice))

	company = get_selected_company()
	if company and si.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(sales_invoice, company))

	payment_entries = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": sales_invoice, "docstatus": 1},
		pluck="parent",
	)
	_ensure_not_deposited(sales_invoice, payment_entries)

	try:
		for pe_name in payment_entries:
			pe = frappe.get_doc("Payment Entry", pe_name)
			if pe.docstatus == 1:
				pe.cancel()

		# Cancelling the Payment Entry(s) above bumps the invoice's own
		# outstanding_amount/modified timestamp — reload before cancelling it,
		# or this throws a stale-timestamp TimestampMismatchError.
		si.reload()
		si.cancel()

		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Express Sales amend failed")
		raise

	return {
		"cancelled": si.name,
		"cancelled_payment_entries": payment_entries,
	}


@frappe.whitelist()
def get_amend_form_data(sales_invoice: str):
	"""The Express Sale form's fields, filled from a cancelled Sales Invoice —
	so ?amend=<name> can reopen the form exactly as the sale was, for the
	admin to correct and re-submit. Payment mode/method are inferred from the
	(now cancelled) Payment Entries that settled the original, so the
	re-submission recreates the same kind of payment by default.
	"""
	ensure_admin()

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 2:
		frappe.throw(_("{0} is not a cancelled Sales Invoice.").format(sales_invoice))

	company = get_selected_company()
	if company and si.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(sales_invoice, company), frappe.PermissionError)

	items = []
	for row in si.items:
		# A "Deduction" row was stored with a negated rate (see
		# _parse_line_items) — round-trip it back to a positive rate + the
		# checkbox ticked so the form shows it the way it was entered.
		rate = flt(row.rate)
		items.append(
			{
				"item": row.item_code,
				"qty": flt(row.qty),
				"rate": abs(rate),
				"description": row.description or "",
				"is_deduction": rate < 0,
			}
		)

	# Payment Entries that referenced this invoice (now cancelled with it).
	pe_names = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": sales_invoice},
		pluck="parent",
	)
	pe_rows = []
	if pe_names:
		pe_rows = frappe.get_all(
			"Payment Entry",
			filters={"name": ["in", list(set(pe_names))]},
			fields=["name", "mode_of_payment", "paid_amount", "reference_no", "remarks"],
		)

	payment_mode = "invoice_only"
	mode_of_payment = ""
	payment_lines = []
	partial_amount = 0
	reference_no = ""
	grand_total = flt(si.grand_total)
	if len(pe_rows) == 1:
		pe = pe_rows[0]
		mode_of_payment = pe.mode_of_payment or ""
		reference_no = pe.reference_no if (pe.reference_no and pe.reference_no != sales_invoice) else ""
		if flt(pe.paid_amount) + 0.01 < grand_total:
			payment_mode = "partial"
			partial_amount = flt(pe.paid_amount)
		else:
			payment_mode = "full"
	elif len(pe_rows) > 1:
		payment_mode = "split"
		payment_lines = [
			{"mode_of_payment": r.mode_of_payment or "", "amount": flt(r.paid_amount)} for r in pe_rows
		]

	return {
		"amended_from": si.name,
		"customer": si.customer,
		"posting_date": str(si.posting_date),
		"remarks": si.remarks or "",
		"payment_terms_template": si.payment_terms_template or "",
		"unique_id": si.custom_unique_id or "",
		"items": items,
		"payment_mode": payment_mode,
		"mode_of_payment": mode_of_payment,
		"partial_amount": partial_amount,
		"payment_lines": payment_lines,
		"reference_no": reference_no,
	}


def _resolve_customer_email(customer: str) -> str | None:
	"""Best email on file for `customer`: the Customer's own native
	`email_id` (a Read Only fetch from `customer_primary_contact.email_id`),
	then `custom_email` (the Student-profile-only field — see
	[[forms-student-customer]] — which can be the only email on file for a
	student even though it's not the native fetch field), then the primary
	address of whichever Contact is actually linked (primary contact if set,
	else the first Contact linked via Dynamic Link, same "resolve via primary
	then first linked" pattern vendor_list.py's own _contacts_for() uses for
	supplier phone numbers).
	"""
	customer_row = frappe.db.get_value(
		"Customer", customer, ["email_id", "custom_email", "customer_primary_contact"], as_dict=True
	)
	if not customer_row:
		return None
	if customer_row.email_id:
		return customer_row.email_id
	if customer_row.custom_email:
		return customer_row.custom_email

	contact_name = customer_row.customer_primary_contact
	if not contact_name:
		contact_name = frappe.db.get_value(
			"Dynamic Link",
			{"parenttype": "Contact", "link_doctype": "Customer", "link_name": customer},
			"parent",
			order_by="creation asc",
		)
	if not contact_name:
		return None

	return frappe.db.get_value(
		"Contact Email", {"parent": contact_name, "is_primary": 1}, "email_id"
	) or frappe.db.get_value("Contact Email", {"parent": contact_name}, "email_id")


@frappe.whitelist()
def send_invoice_email(sales_invoice: str):
	"""Email a submitted Sales Invoice's PDF straight to the customer on
	file — the "Send Invoice" button in Sales Invoice History. Uses the same
	Print Format this page's own "Invoice" print link already uses (the
	company's configured default from Forms Print Settings, falling back to
	"Sales Invoice with Remarks"), so the PDF the customer gets matches
	exactly what an admin would see clicking Print here.
	"""
	ensure_admin()

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted Sales Invoice.").format(sales_invoice))

	company = get_selected_company()
	if company and si.company != company:
		frappe.throw(_("{0} does not belong to {1}.").format(sales_invoice, company))

	email = _resolve_customer_email(si.customer)
	if not email:
		frappe.throw(_("{0} has no email address on file.").format(si.customer_name or si.customer))

	print_format = get_print_format_settings(si.company).get("sales_invoice_format") or "Sales Invoice with Remarks"
	pdf_content = frappe.get_print(si.doctype, si.name, print_format=print_format, as_pdf=True)

	frappe.sendmail(
		recipients=[email],
		subject=_("Invoice {0} from {1}").format(si.name, si.company),
		message=_(
			"Dear {0},<br><br>Please find attached your invoice {1} for {2}.<br><br>Thank you."
		).format(
			si.customer_name or si.customer,
			si.name,
			frappe.utils.fmt_money(si.grand_total, currency=si.currency),
		),
		attachments=[{"fname": f"{si.name}.pdf", "fcontent": pdf_content}],
		reference_doctype=si.doctype,
		reference_name=si.name,
	)

	return {"email": email}

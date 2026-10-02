# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.www.printview import get_rendered_template

from ez_accounting.api import ensure_admin

no_cache = 1


def get_context(context):
	"""Bare-content companion to /forms/user/printer: renders *just* one
	page's worth of printable content, with no site chrome and none of
	/desk/print's own on-screen "gutter" wrapper (.print-format-gutter >
	.print-format, with its own max-width/padding meant for a comfortable
	browser preview, not a true-to-size one — see [[forms-express-purchase]]'s
	FHB Check margin-extraction writeup for why that wrapper's sizing doesn't
	perfectly match what actually prints). Meant to be iframed (by
	/forms/user/printer) at 8.5in x 11in, so what's shown is exactly the
	content's own markup/CSS with nothing else influencing its size —
	printerhtml.html itself is a bare template (no {% extends %}) that
	outputs only this rendered content, nothing else.

	Two kinds of content, both ending up as one `context.rendered_html`
	string so printerhtml.html doesn't need to know which one it got:

	- The default: a real document's own Print Format (`id` + `format` query
	  params) -- doctype is deliberately not a separate param, since a Print
	  Format always belongs to exactly one doc_type, so `format` alone is
	  enough to resolve it (the sibling /forms/user/printer page does its own
	  doctype *detection* from just an ID; this page skips that once it
	  already has a format).
	"""
	ensure_admin()

	kind = (frappe.form_dict.get("kind") or "").strip()
	if kind == "aged_receivables":
		context.rendered_html = _render_aged_receivables()
		return context
	if kind == "aged_payables":
		context.rendered_html = _render_aged_payables()
		return context
	if kind == "cash_receipts_journal":
		context.rendered_html = _render_cash_receipts_journal()
		return context
	if kind == "cash_disbursements_journal":
		context.rendered_html = _render_cash_disbursements_journal()
		return context
	if kind == "check_register":
		context.rendered_html = _render_check_register()
		return context
	if kind == "customer_ledgers":
		context.rendered_html = _render_customer_ledgers()
		return context
	if kind == "vendor_ledgers":
		context.rendered_html = _render_vendor_ledgers()
		return context
	if kind == "vendor_list":
		context.rendered_html = _render_vendor_list()
		return context
	if kind == "sales_journal":
		context.rendered_html = _render_sales_journal()
		return context
	if kind == "purchase_journal":
		context.rendered_html = _render_purchase_journal()
		return context
	if kind == "vendor_1099_report":
		context.rendered_html = _render_vendor_1099_report()
		return context
	if kind == "payments":
		context.rendered_html = _render_payments()
		return context
	if kind == "salesreport":
		context.rendered_html = _render_sales_report()
		return context
	if kind == "deposits":
		context.rendered_html = _render_deposits()
		return context
	if kind == "reconciliation":
		context.rendered_html = _render_reconciliation()
		return context

	name = (frappe.form_dict.get("id") or "").strip()
	print_format_name = (frappe.form_dict.get("format") or "").strip()

	if not name or not print_format_name:
		frappe.throw(_("Both id and format are required."))

	print_format = frappe.get_doc("Print Format", print_format_name)
	if not print_format.doc_type:
		frappe.throw(_("{0} is not tied to a doctype.").format(print_format_name))

	doc = frappe.get_doc(print_format.doc_type, name)
	if not doc.has_permission("print"):
		frappe.throw(_("Not permitted to print this document."), frappe.PermissionError)

	# Documents retain the letter head selected when they were created.  The
	# Forms printer should instead honour the company's current default so a
	# letterhead update applies consistently to old and new documents, across
	# every available Print Format.
	company_letterhead = None
	if doc.meta.has_field("company") and doc.get("company"):
		company_letterhead = frappe.get_cached_value("Company", doc.company, "default_letter_head")

	context.rendered_html = get_rendered_template(
		doc,
		print_format=print_format,
		meta=None,
		no_letterhead=0,
		letterhead=company_letterhead,
	)
	return context


def _render_reconciliation():
	from ez_accounting.www.forms.user.reconciliation import build_reconciliation_report

	account = (frappe.form_dict.get("account") or "").strip()
	as_of_date = (frappe.form_dict.get("as_of_date") or "").strip()
	statement_balance = frappe.form_dict.get("statement_balance") or 0
	report = build_reconciliation_report(account, as_of_date, statement_balance)
	report.update({"fmt_money": frappe.utils.fmt_money, "format_date": frappe.utils.format_date})
	return frappe.render_template("templates/includes/reconciliation_statement.html", report)


def _format_qty(value) -> str:
	"""Plain (non-currency) quantity string: whole numbers print without a
	decimal point (the common case -- 1 registration fee, 3 textbooks), a
	fractional qty keeps up to 2 decimal places. frappe.utils.fmt_money()
	handles the currency-formatted numbers on this statement (Rate/Amount);
	qty isn't a currency, so it gets its own tiny formatter instead of
	repurposing that one with an empty currency symbol.
	"""
	value = frappe.utils.flt(value)
	if value == int(value):
		return str(int(value))
	return f"{value:.2f}"


def _render_vendor_1099_report():
	"""The printed "1099 Vendor Report" for the selected company —
	`kind=vendor_1099_report` (+ `year`). Same data as the on-screen
	/forms/user/vendor_1099 report; run through
	templates/includes/vendor_1099_statement.html.
	"""
	from ez_accounting.api import get_selected_company
	from ez_accounting.www.forms.user.vendor_1099 import _build_report

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	year = (frappe.form_dict.get("year") or "").strip()
	if not (year.isdigit() and len(year) == 4):
		year = frappe.utils.nowdate()[:4]

	data = _build_report(company, year)
	fmt = frappe.utils.fmt_money
	fmt_date = frappe.utils.format_date

	vendors = []
	for v in data["vendors"]:
		vendors.append(
			{
				"vendor_id": v["vendor_id"],
				"vendor": v["vendor"],
				"kind": v["kind"],
				"box": v["box"],
				"box_desc": v["box_desc"],
				"limit_met": v["limit_met"],
				"subtotal": fmt(v["subtotal"]),
				"rows": [
					{"date": fmt_date(r["date"]), "trans_no": r["trans_no"], "amount": fmt(r["amount"])}
					for r in v["rows"]
				],
			}
		)

	return frappe.render_template(
		"templates/includes/vendor_1099_statement.html",
		{
			"company": company,
			"year": year,
			"vendors": vendors,
			"report_total": fmt(data["total"]),
			"printed_at": frappe.utils.format_datetime(
				frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"
			),
		},
	)


def _render_purchase_journal():
	"""The printed "Purchase Journal" — `kind=purchase_journal` (+ `from_date` /
	`to_date`). The buy-side mirror of _render_sales_journal(); same data as the
	on-screen /forms/user/purchase_journal report.
	"""
	from ez_accounting.api import get_date_range_with_default, get_selected_company
	from ez_accounting.www.forms.user.purchase_journal import _build_purchase_journal

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	from_date, to_date = get_date_range_with_default()
	data = _build_purchase_journal(company, from_date, to_date)

	fmt = frappe.utils.fmt_money
	fmt_date = frappe.utils.format_date

	vouchers = []
	for block in data["vouchers"]:
		vouchers.append(
			{
				"date": fmt_date(block["date"]),
				"invoice": block["invoice"],
				"lines": [
					{
						"account_id": line["account_id"],
						"account_name": line["account_name"],
						"line_description": line["line_description"],
						"debit": fmt(line["debit"]) if line["debit"] else "",
						"credit": fmt(line["credit"]) if line["credit"] else "",
					}
					for line in block["lines"]
				],
			}
		)

	period = _("From {0} to {1}").format(
		fmt_date(from_date, "MMM d, yyyy"), fmt_date(to_date, "MMM d, yyyy")
	)

	return frappe.render_template(
		"templates/includes/purchase_journal_statement.html",
		{
			"company": company,
			"period": period,
			"vouchers": vouchers,
			"total_debit": fmt(data["total_debit"]),
			"total_credit": fmt(data["total_credit"]),
			"printed_at": frappe.utils.format_datetime(
				frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"
			),
		},
	)


def _render_sales_journal():
	"""The printed "Sales Journal" for the selected company — `kind=sales_journal`
	(+ `from_date` / `to_date`). Not a real doctype / Print Format, same as the
	other `kind=` printouts: this app's own computed journal run through a plain
	template (templates/includes/sales_journal_statement.html).

	One block per Sales Invoice (its full set of GL legs), the way EIS's old
	Peachtree "Sales Journal" printed. Same data as the on-screen
	/forms/user/sales_journal report — see sales_journal._build_sales_journal().
	"""
	from ez_accounting.api import get_date_range_with_default, get_selected_company
	from ez_accounting.www.forms.user.sales_journal import _build_sales_journal

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	from_date, to_date = get_date_range_with_default()
	data = _build_sales_journal(company, from_date, to_date)

	fmt = frappe.utils.fmt_money
	fmt_date = frappe.utils.format_date

	vouchers = []
	for block in data["vouchers"]:
		vouchers.append(
			{
				"date": fmt_date(block["date"]),
				"invoice": block["invoice"],
				"lines": [
					{
						"account_id": line["account_id"],
						"account_name": line["account_name"],
						"line_description": line["line_description"],
						"debit": fmt(line["debit"]) if line["debit"] else "",
						"credit": fmt(line["credit"]) if line["credit"] else "",
					}
					for line in block["lines"]
				],
			}
		)

	period = _("From {0} to {1}").format(
		fmt_date(from_date, "MMM d, yyyy"), fmt_date(to_date, "MMM d, yyyy")
	)

	return frappe.render_template(
		"templates/includes/sales_journal_statement.html",
		{
			"company": company,
			"period": period,
			"vouchers": vouchers,
			"total_debit": fmt(data["total_debit"]),
			"total_credit": fmt(data["total_credit"]),
			"printed_at": frappe.utils.format_datetime(
				frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"
			),
		},
	)


def _render_vendor_list():
	"""The printed "Vendor List" — `kind=vendor_list`. A directory of active
	suppliers; same data as the on-screen /forms/user/vendor_list report.
	"""
	from ez_accounting.api import get_selected_company
	from ez_accounting.www.forms.user.vendor_list import _build_vendor_list

	return frappe.render_template(
		"templates/includes/vendor_list_statement.html",
		{
			"company": get_selected_company() or "",
			"vendors": _build_vendor_list(),
			"printed_at": frappe.utils.format_datetime(
				frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"
			),
		},
	)


def _render_vendor_ledgers():
	"""The printed "Vendor Ledgers" report — `kind=vendor_ledgers` (+
	`from_date` / `to_date`). The payables mirror of _render_customer_ledgers();
	same data as the on-screen /forms/user/vendor_ledgers report.
	"""
	from ez_accounting.api import get_date_range_with_default, get_selected_company
	from ez_accounting.www.forms.user.vendor_ledgers import _build_vendor_ledgers

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	from_date, to_date = get_date_range_with_default()
	data = _build_vendor_ledgers(company, from_date, to_date)

	fmt = frappe.utils.fmt_money
	fmt_date = frappe.utils.format_date

	vendors = []
	for v in data["vendors"]:
		vendors.append(
			{
				"vendor_id": v["vendor_id"],
				"vendor": v["vendor"],
				"opening": fmt(v["opening"]),
				"no_activity": v["no_activity"],
				"rows": [
					{
						"date": fmt_date(row["date"]),
						"trans_no": row["trans_no"],
						"type": row["type"],
						"debit": fmt(row["debit"]) if row["debit"] else "",
						"credit": fmt(row["credit"]) if row["credit"] else "",
						"balance": fmt(row["balance"]),
					}
					for row in v["rows"]
				],
			}
		)

	period = _("From {0} to {1}").format(
		fmt_date(from_date, "MMM d, yyyy"), fmt_date(to_date, "MMM d, yyyy")
	)

	return frappe.render_template(
		"templates/includes/vendor_ledgers_statement.html",
		{
			"company": company,
			"period": period,
			"vendors": vendors,
			"total_debit": fmt(data["total_debit"]),
			"total_credit": fmt(data["total_credit"]),
			"total_balance": fmt(data["total_balance"]),
			"printed_at": frappe.utils.format_datetime(
				frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"
			),
		},
	)


def _render_customer_ledgers():
	"""The printed "Customer Ledgers" report for the selected company —
	`kind=customer_ledgers` (+ `from_date` / `to_date`). Not a real doctype /
	Print Format, same as the other `kind=` printouts: this app's own computed
	ledger run through a plain template
	(templates/includes/customer_ledgers_statement.html).

	One block per customer (Balance Fwd line + every receivable transaction
	with a running balance), the way EIS's old Peachtree "Customer Ledgers"
	printed. Same data as the on-screen /forms/user/customer_ledgers report —
	see customer_ledgers._build_customer_ledgers().
	"""
	from ez_accounting.api import get_date_range_with_default, get_selected_company
	from ez_accounting.www.forms.user.customer_ledgers import _build_customer_ledgers

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	from_date, to_date = get_date_range_with_default()
	data = _build_customer_ledgers(company, from_date, to_date)

	fmt = frappe.utils.fmt_money
	fmt_date = frappe.utils.format_date

	customers = []
	for c in data["customers"]:
		customers.append(
			{
				"customer_id": c["customer_id"],
				"customer": c["customer"],
				"opening": fmt(c["opening"]),
				"no_activity": c["no_activity"],
				"rows": [
					{
						"date": fmt_date(row["date"]),
						"trans_no": row["trans_no"],
						"type": row["type"],
						"debit": fmt(row["debit"]) if row["debit"] else "",
						"credit": fmt(row["credit"]) if row["credit"] else "",
						"balance": fmt(row["balance"]),
					}
					for row in c["rows"]
				],
			}
		)

	period = _("From {0} to {1}").format(
		fmt_date(from_date, "MMM d, yyyy"), fmt_date(to_date, "MMM d, yyyy")
	)

	return frappe.render_template(
		"templates/includes/customer_ledgers_statement.html",
		{
			"company": company,
			"period": period,
			"customers": customers,
			"total_debit": fmt(data["total_debit"]),
			"total_credit": fmt(data["total_credit"]),
			"total_balance": fmt(data["total_balance"]),
			"printed_at": frappe.utils.format_datetime(
				frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"
			),
		},
	)


def _render_check_register():
	"""The printed "Check Register" — `kind=check_register` (+ `from_date` /
	`to_date`). One row per check; same data as the on-screen
	/forms/user/check_register report.
	"""
	from ez_accounting.api import get_date_range_with_default, get_selected_company
	from ez_accounting.www.forms.user.check_register import _build_check_register

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	from_date, to_date = get_date_range_with_default()
	data = _build_check_register(company, from_date, to_date)

	fmt = frappe.utils.fmt_money
	fmt_date = frappe.utils.format_date

	checks = [
		{
			"check_no": c["check_no"],
			"date": fmt_date(c["date"]),
			"payee": c["payee"],
			"cash_account": c["cash_account"],
			"amount": "" if c["void"] else fmt(c["amount"]),
			"void": c["void"],
		}
		for c in data["checks"]
	]

	period = _("From {0} to {1}").format(
		fmt_date(from_date, "MMM d, yyyy"), fmt_date(to_date, "MMM d, yyyy")
	)

	return frappe.render_template(
		"templates/includes/check_register_statement.html",
		{
			"company": company,
			"period": period,
			"checks": checks,
			"report_total": fmt(data["total"]),
			"printed_at": frappe.utils.format_datetime(
				frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"
			),
		},
	)


def _render_cash_disbursements_journal():
	"""The printed "Cash Disbursements Journal" — `kind=cash_disbursements_journal`
	(+ `from_date` / `to_date`, or `check_no` to print one check regardless
	of date). The credit-side mirror of
	_render_cash_receipts_journal(); same data as the on-screen
	/forms/user/cash_disbursements report.
	"""
	from ez_accounting.api import get_date_range_with_default, get_selected_company
	from ez_accounting.www.forms.user.cash_disbursements import _build_cash_disbursements

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	from_date, to_date = get_date_range_with_default()
	check_no = (frappe.form_dict.get("check_no") or "").strip()
	data = _build_cash_disbursements(company, from_date, to_date, check_no)

	fmt = frappe.utils.fmt_money
	fmt_date = frappe.utils.format_date

	vouchers = []
	for block in data["vouchers"]:
		vouchers.append(
			{
				"date": fmt_date(block["date"]),
				"check_no": block["check_no"],
				"lines": [
					{
						"account_id": line["account_id"],
						"account_name": line["account_name"],
						"line_description": line["line_description"],
						"debit": fmt(line["debit"]) if line["debit"] else "",
						"credit": fmt(line["credit"]) if line["credit"] else "",
					}
					for line in block["lines"]
				],
			}
		)

	period = (
		_("Check No. {0}").format(check_no)
		if check_no
		else _("From {0} to {1}").format(
			fmt_date(from_date, "MMM d, yyyy"), fmt_date(to_date, "MMM d, yyyy")
		)
	)

	return frappe.render_template(
		"templates/includes/cash_disbursements_journal_statement.html",
		{
			"company": company,
			"period": period,
			"vouchers": vouchers,
			"total_debit": fmt(data["total_debit"]),
			"total_credit": fmt(data["total_credit"]),
			"printed_at": frappe.utils.format_datetime(
				frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"
			),
		},
	)


def _render_cash_receipts_journal():
	"""The printed "Cash Receipts Journal" for the selected company —
	`kind=cash_receipts_journal` (+ `from_date` / `to_date`). Not a real
	doctype / Print Format, same as the other `kind=` printouts: this app's
	own computed journal run through a plain template
	(templates/includes/cash_receipts_journal_statement.html), so it goes
	through the same true-to-size preview + print() flow.

	One block per voucher (its full set of GL legs), the way EIS's old
	Peachtree "Cash Receipts Journal" printed. Same data as the on-screen
	/forms/user/cash_receipts report — see cash_receipts._build_cash_receipts().
	"""
	from ez_accounting.api import get_date_range_with_default, get_selected_company
	from ez_accounting.www.forms.user.cash_receipts import _build_cash_receipts

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	from_date, to_date = get_date_range_with_default()
	data = _build_cash_receipts(company, from_date, to_date)

	fmt = frappe.utils.fmt_money
	fmt_date = frappe.utils.format_date

	vouchers = []
	for block in data["vouchers"]:
		vouchers.append(
			{
				"date": fmt_date(block["date"]),
				"lines": [
					{
						"account_id": line["account_id"],
						"account_name": line["account_name"],
						"ref": line["ref"],
						"line_description": line["line_description"],
						"debit": fmt(line["debit"]) if line["debit"] else "",
						"credit": fmt(line["credit"]) if line["credit"] else "",
					}
					for line in block["lines"]
				],
			}
		)

	period = _("From {0} to {1}").format(
		fmt_date(from_date, "MMM d, yyyy"), fmt_date(to_date, "MMM d, yyyy")
	)

	return frappe.render_template(
		"templates/includes/cash_receipts_journal_statement.html",
		{
			"company": company,
			"period": period,
			"vouchers": vouchers,
			"total_debit": fmt(data["total_debit"]),
			"total_credit": fmt(data["total_credit"]),
			"printed_at": frappe.utils.format_datetime(
				frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"
			),
		},
	)


def _render_aged_payables():
	"""The printed "Aged Payables" statement for the selected company —
	`kind=aged_payables` (+ optional `as_of`). The payables mirror of
	_render_aged_receivables(): one row per unpaid Purchase Invoice grouped
	under its vendor with an underlined subtotal, then a Report Total. Same
	aging (due_date, else posting_date) collapsed to plain 30-day periods
	(ap.PRINT_BUCKETS). Debit notes carry a negative Amount Due and net into
	the totals.
	"""
	from ez_accounting.api import apply_permitted_filter, get_selected_company
	from ez_accounting.www.forms.user.ap import PRINT_BUCKET_KEYS, PRINT_BUCKETS, print_bucket_for

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	as_of = (frappe.form_dict.get("as_of") or frappe.utils.nowdate()).strip()
	as_of_date = frappe.utils.getdate(as_of)

	filters = [
		["company", "=", company],
		["docstatus", "=", 1],
		["outstanding_amount", "!=", 0],
	]
	filters = apply_permitted_filter(filters, "Supplier", "supplier")
	filters = apply_permitted_filter(filters, "Company", "company")

	rows = frappe.get_all(
		"Purchase Invoice",
		filters=filters,
		fields=["name", "supplier", "supplier_name", "posting_date", "due_date", "bill_no", "remarks", "outstanding_amount"],
		order_by="supplier_name asc, posting_date asc",
	)

	fmt = frappe.utils.fmt_money
	empty_cells = {k: "" for k in PRINT_BUCKET_KEYS}

	groups: dict[str, dict] = {}
	grand = {k: 0.0 for k in PRINT_BUCKET_KEYS}
	grand["amount_due"] = 0.0

	for r in rows:
		aging_date = frappe.utils.getdate(r.due_date or r.posting_date)
		bkey = print_bucket_for((as_of_date - aging_date).days)
		amount = frappe.utils.flt(r.outstanding_amount)

		group = groups.setdefault(
			r.supplier,
			{
				"vendor_id": r.supplier,
				"vendor_name": r.supplier_name or r.supplier,
				"invoices": [],
				"subtotal": {**{k: 0.0 for k in PRINT_BUCKET_KEYS}, "amount_due": 0.0},
			},
		)

		description = (r.bill_no or "").strip() or (r.remarks or "").strip()
		if len(description) > 60:
			description = description[:57] + "…"

		group["invoices"].append(
			{
				"invoice": r.name,
				"description": description or None,
				**{**empty_cells, bkey: fmt(amount)},
				"amount_due": fmt(amount),
			}
		)
		group["subtotal"][bkey] += amount
		group["subtotal"]["amount_due"] += amount
		grand[bkey] += amount
		grand["amount_due"] += amount

	group_list = sorted(groups.values(), key=lambda g: g["vendor_name"].lower())
	for group in group_list:
		group["subtotal"] = {
			k: (fmt(v) if (v or k == "amount_due") else "") for k, v in group["subtotal"].items()
		}

	return frappe.render_template(
		"templates/includes/aged_payables_statement.html",
		{
			"company": company,
			"as_of": frappe.utils.format_date(as_of, "MMM d, yyyy"),
			"buckets": PRINT_BUCKETS,
			"groups": group_list,
			"grand_total": {k: (fmt(v) if (v or k == "amount_due") else "") for k, v in grand.items()},
			"printed_at": frappe.utils.format_datetime(
				frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"
			),
		},
	)


def _render_aged_receivables():
	"""The printed "Aged Receivables" statement for the selected company —
	`kind=aged_receivables` (+ optional `as_of`). Not a real doctype / Print
	Format, same as kind=statement above: this app's own computed aging run
	through a plain template (templates/includes/aged_receivables_statement.html),
	so it goes through the same true-to-size preview + print() flow as every
	document in this app.

	One row per outstanding Sales Invoice, grouped under its customer with an
	underlined subtotal, then a grand total — the layout EIS's old accounting
	package produced. Aging matches the on-screen /forms/user/ar report
	(due_date, falling back to posting_date), just collapsed to plain 30-day
	periods with no separate "Current" column (see ar.PRINT_BUCKETS).
	"""
	from ez_accounting.api import apply_permitted_filter, get_selected_company
	from ez_accounting.www.forms.user.ar import PRINT_BUCKETS, PRINT_BUCKET_KEYS, print_bucket_for

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	as_of = (frappe.form_dict.get("as_of") or frappe.utils.nowdate()).strip()
	as_of_date = frappe.utils.getdate(as_of)

	filters = [
		["company", "=", company],
		["docstatus", "=", 1],
		["outstanding_amount", ">", 0],
	]
	filters = apply_permitted_filter(filters, "Customer", "customer")
	filters = apply_permitted_filter(filters, "Company", "company")

	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=[
			"name",
			"customer",
			"customer_name",
			"posting_date",
			"due_date",
			"po_no",
			"remarks",
			"outstanding_amount",
		],
		order_by="customer_name asc, posting_date asc",
	)

	fmt = frappe.utils.fmt_money
	fmt_date = frappe.utils.format_date
	empty_cells = {k: "" for k in PRINT_BUCKET_KEYS}

	# customer docname -> group. Keyed by the docname (the "Customer ID"
	# column) so two customers with the same display name don't merge.
	groups: dict[str, dict] = {}
	grand = {k: 0.0 for k in PRINT_BUCKET_KEYS}
	grand["amount_due"] = 0.0

	for r in rows:
		aging_date = frappe.utils.getdate(r.due_date or r.posting_date)
		days_past_due = (as_of_date - aging_date).days
		bkey = print_bucket_for(days_past_due)
		amount = frappe.utils.flt(r.outstanding_amount)

		group = groups.setdefault(
			r.customer,
			{
				"customer_id": r.customer,
				"customer_name": r.customer_name or r.customer,
				"invoices": [],
				"subtotal": {**{k: 0.0 for k in PRINT_BUCKET_KEYS}, "amount_due": 0.0},
			},
		)

		# The reference statement shows a short free-text description beside the
		# invoice number ("MARCH 2020 TF", "SY14-15") — that's the invoice's own
		# PO No (Express Sales writes the "Unique ID" there) or, failing that,
		# its Remarks. Trimmed so a long remark can't blow out the column.
		description = (r.po_no or "").strip() or (r.remarks or "").strip()
		if len(description) > 60:
			description = description[:57] + "…"

		group["invoices"].append(
			{
				"invoice": r.name,
				"description": description or None,
				"posting_date": fmt_date(r.posting_date),
				**{**empty_cells, bkey: fmt(amount)},
				"amount_due": fmt(amount),
			}
		)
		group["subtotal"][bkey] += amount
		group["subtotal"]["amount_due"] += amount
		grand[bkey] += amount
		grand["amount_due"] += amount

	group_list = sorted(groups.values(), key=lambda g: g["customer_name"].lower())
	for group in group_list:
		group["subtotal"] = {
			k: (fmt(v) if (v or k == "amount_due") else "") for k, v in group["subtotal"].items()
		}

	return frappe.render_template(
		"templates/includes/aged_receivables_statement.html",
		{
			"company": company,
			"as_of": fmt_date(as_of, "MMM d, yyyy"),
			"buckets": PRINT_BUCKETS,
			"groups": group_list,
			"grand_total": {
				k: (fmt(v) if (v or k == "amount_due") else "") for k, v in grand.items()
			},
			"printed_at": frappe.utils.format_datetime(
				frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"
			),
		},
	)


def _get_company_letter_head_html(company: str, doctype_label: str = "", name: str = "") -> str | None:
	"""Renders a Company's own default Letter Head (its Company Setup
	letterhead -- logo + address + contact block, e.g. "Company Letterhead -
	Grey") the same way a real Print Format does (mirrors
	frappe.www.printview.get_rendered_template()'s own letter-head handling:
	fetch the Letter Head's `content` -- itself Jinja source, not plain HTML
	-- then render it with a `doc` in context). Used by `kind=` printouts
	that aren't tied to any single Document but still want the same company
	letterhead a real invoice prints with. `doctype_label`/`name` feed the
	letterhead's own top-right corner (a real invoice's own doctype/number
	there) -- pass whatever identifies this printout instead, or leave both
	blank to omit that corner's text entirely.
	"""
	if not company:
		return None

	letterhead_name = frappe.get_cached_value("Company", company, "default_letter_head")
	if not letterhead_name:
		letterhead_name = frappe.db.get_value("Letter Head", {"is_default": 1}, "name")
	if not letterhead_name:
		return None

	content = frappe.db.get_value("Letter Head", letterhead_name, "content")
	if not content:
		return None

	from frappe.contacts.doctype.address.address import get_default_address

	company_address_name = get_default_address("Company", company)
	pseudo_doc = frappe._dict(
		{
			"company": company,
			"company_address": company_address_name,
			"billing_address": company_address_name,
			"doctype": doctype_label,
			"name": name,
		}
	)
	return frappe.render_template(content, {"doc": pseudo_doc})


def _render_payments():
	"""The printed "Payments" report — `kind=payments` (+ optional `from_date`,
	`to_date`, `doc_id`, and `owner`). Same data (and the same _build_payments() call) as the
	on-screen /forms/user/payments report; no default period is imposed —
	an unset date range prints exactly what the on-screen page itself would
	show with no filter applied (the whole company register)."""
	from ez_accounting.api import get_selected_company
	from ez_accounting.www.forms.user.payments import _build_payments

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	from_date = (frappe.form_dict.get("from_date") or "").strip()
	to_date = (frappe.form_dict.get("to_date") or "").strip()
	doc_id = (frappe.form_dict.get("doc_id") or "").strip()
	owner = (frappe.form_dict.get("owner") or "").strip()
	payment_type = (frappe.form_dict.get("payment_type") or "Receive").strip()
	rows = _build_payments(company, from_date, to_date, doc_id=doc_id, owner=owner, payment_type=payment_type)

	fmt = frappe.utils.fmt_money
	fmt_date = frappe.utils.format_date

	payments = [
		{
			"sequence_number": r["custom_sequence_number"] or "—",
			# Posting Date = the transaction's own (often backdated) date;
			# Created = when it was actually recorded here, which is what the
			# Sequence No itself is ordered by -- see [[forms-payments-sequence]].
			"posting_date": fmt_date(r["posting_date"]),
			"created": frappe.utils.format_datetime(r["creation"], "M/d/yyyy h:mm a"),
			"created_by": r["created_by"],
			"type": r["payment_type"],
			"party": r["party_name"] or r["party_type"] or "",
			"amount": fmt(r["paid_amount"], currency=r["currency"]),
			"unallocated": fmt(r["unallocated_amount"], currency=r["currency"]) if r["unallocated_amount"] else "",
			"mode_of_payment": r["mode_of_payment"] or "",
			"reference_no": r["reference_no"] or "",
			"status": r["status"],
			"cancelled": r["status"] == "Cancelled",
		}
		for r in rows
	]

	if from_date and to_date:
		period = _("From {0} to {1}").format(fmt_date(from_date), fmt_date(to_date))
	elif from_date:
		period = _("From {0}").format(fmt_date(from_date))
	elif to_date:
		period = _("Through {0}").format(fmt_date(to_date))
	else:
		period = _("All payments")
	if payment_type and payment_type != "All":
		period = _("{0} · Type: {1}").format(period, payment_type)

	return frappe.render_template(
		"templates/includes/payments_statement.html",
		{
			"company": company,
			"period": period,
			"payments": payments,
			"printed_at": frappe.utils.format_datetime(frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"),
		},
	)


def _render_sales_report():
	"""Printable counterpart of /forms/user/salesreport."""
	from ez_accounting.api import get_selected_company
	from ez_accounting.www.forms.user.salesreport import _build_sales_report, _sales_summary

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))
	from_date = (frappe.form_dict.get("from_date") or "").strip()
	to_date = (frappe.form_dict.get("to_date") or "").strip()
	search = (frappe.form_dict.get("search") or "").strip()
	deposit_day = (frappe.form_dict.get("deposit_day") or "").strip()
	rows = _build_sales_report(company, from_date, to_date, search, deposit_day)
	summary = _sales_summary(rows)

	if from_date and to_date:
		period = _("From {0} to {1}").format(frappe.utils.format_date(from_date), frappe.utils.format_date(to_date))
	elif from_date:
		period = _("From {0}").format(frappe.utils.format_date(from_date))
	elif to_date:
		period = _("Through {0}").format(frappe.utils.format_date(to_date))
	else:
		period = _("All submitted sales")
	if search:
		period = _("{0} · Search: {1}").format(period, search)
	if deposit_day:
		period = _("Deposit Day: {0}").format(frappe.utils.format_date(deposit_day))

	return frappe.render_template("templates/includes/sales_report_statement.html", {
		"company": company,
		"period": period,
		"rows": rows,
		"summary": summary,
		"fmt_money": frappe.utils.fmt_money,
		"printed_at": frappe.utils.format_datetime(frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"),
	})


def _render_deposits():
	"""The printed "Deposits" report — `kind=deposits` (+ optional `from_date`
	/ `to_date`). Same data (and the same _build_deposits() call) as the
	on-screen /forms/user/deposits report; no default period is imposed."""
	from ez_accounting.api import get_selected_company
	from ez_accounting.www.forms.user.deposits import _build_deposits

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	from_date = (frappe.form_dict.get("from_date") or "").strip()
	to_date = (frappe.form_dict.get("to_date") or "").strip()
	rows = _build_deposits(company, from_date, to_date)

	fmt = frappe.utils.fmt_money
	fmt_date = frappe.utils.format_date

	deposits = [
		{
			"sequence_number": r["custom_sequence_number"] or "—",
			"deposit_date": fmt_date(r["deposit_date"]),
			"created": frappe.utils.format_datetime(r["creation"], "M/d/yyyy h:mm a"),
			"total_deposit": fmt(r["total_deposit"], currency=r["currency"]),
			"variance": fmt(r["variance"], currency=r["currency"]) if r["variance"] else "",
			"check_variance": fmt(r["check_variance"], currency=r["currency"]) if r["check_variance"] else "",
			"status": r["status"],
			"cancelled": r["status"] == "Cancelled",
		}
		for r in rows
	]

	if from_date and to_date:
		period = _("From {0} to {1}").format(fmt_date(from_date), fmt_date(to_date))
	elif from_date:
		period = _("From {0}").format(fmt_date(from_date))
	elif to_date:
		period = _("Through {0}").format(fmt_date(to_date))
	else:
		period = _("All deposits")

	return frappe.render_template(
		"templates/includes/deposits_statement.html",
		{
			"company": company,
			"period": period,
			"deposits": deposits,
			"printed_at": frappe.utils.format_datetime(frappe.utils.now_datetime(), "M/d/yyyy 'at' h:mm a"),
		},
	)

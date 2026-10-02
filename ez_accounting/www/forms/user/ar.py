# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from ez_accounting.api import (
	apply_permitted_filter,
	ensure_logged_in,
	get_selected_company,
)

no_cache = 1

# Aging buckets, in order: (key, label). Labels are translated at render time
# (template `_(...)`), not here at import — frappe._ has no translation context
# at module load. "Current" = anything not yet past its due date.
BUCKETS = [
	("current", "Current"),
	("b1_30", "1–30"),
	("b31_60", "31–60"),
	("b61_90", "61–90"),
	("b90_plus", "90+"),
]
BUCKET_KEYS = [b[0] for b in BUCKETS]
BUCKET_LABELS = dict(BUCKETS)


def _bucket_for(days_past_due: int) -> str:
	if days_past_due <= 0:
		return "current"
	if days_past_due <= 30:
		return "b1_30"
	if days_past_due <= 60:
		return "b31_60"
	if days_past_due <= 90:
		return "b61_90"
	return "b90_plus"


# The printed "Aged Receivables" statement (/forms/user/printer?kind=aged_receivables,
# rendered by printerhtml._render_aged_receivables) uses plain 30-day periods
# with no separate "Current" column — not-yet-due invoices fold into "0–30" —
# to match the standard printed A/R aging layout EIS has always used. Same
# underlying aging date as the on-screen report (due_date, else posting_date).
PRINT_BUCKETS = [
	("p0_30", "0–30"),
	("p31_60", "31–60"),
	("p61_90", "61–90"),
	("p_over_90", "Over 90 days"),
]
PRINT_BUCKET_KEYS = [b[0] for b in PRINT_BUCKETS]


def print_bucket_for(days_past_due: int) -> str:
	if days_past_due <= 30:
		return "p0_30"
	if days_past_due <= 60:
		return "p31_60"
	if days_past_due <= 90:
		return "p61_90"
	return "p_over_90"


def _build_aging(company: str, as_of: str) -> dict:
	"""Every submitted Sales Invoice for `company` with outstanding money still
	on it, aged as of `as_of`. Plain frappe.get_all() read summed in Python —
	same "no new SQL aggregate" discipline the Dashboard's own outstanding
	Sales card already uses, just bucketed by age here.

	Aging is off `due_date` (falling back to `posting_date` when an invoice has
	no due date), not posting date — an invoice inside its payment terms isn't
	overdue.
	"""
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
			"grand_total",
			"outstanding_amount",
			"currency",
		],
		order_by="customer_name asc, due_date asc, posting_date asc",
	)

	as_of_date = getdate(as_of)
	invoices = []
	# customer name -> {label, total, <bucket>: amount, ...}
	by_customer: dict[str, dict] = {}
	totals = {k: 0.0 for k in BUCKET_KEYS}
	totals["total"] = 0.0

	for r in rows:
		aging_date = getdate(r.due_date or r.posting_date)
		days_past_due = (as_of_date - aging_date).days
		bucket = _bucket_for(days_past_due)
		outstanding = flt(r.outstanding_amount)

		invoices.append(
			{
				"name": r.name,
				"customer": r.customer_name or r.customer,
				"posting_date": r.posting_date,
				"due_date": r.due_date,
				"days_past_due": max(days_past_due, 0),
				"bucket": bucket,
				"bucket_label": BUCKET_LABELS[bucket],
				"grand_total": flt(r.grand_total),
				"outstanding_amount": outstanding,
			}
		)

		cust = by_customer.setdefault(
			r.customer_name or r.customer,
			{"customer": r.customer_name or r.customer, "total": 0.0, **{k: 0.0 for k in BUCKET_KEYS}},
		)
		cust[bucket] += outstanding
		cust["total"] += outstanding

		totals[bucket] += outstanding
		totals["total"] += outstanding

	customers = sorted(by_customer.values(), key=lambda c: c["total"], reverse=True)
	return {"invoices": invoices, "customers": customers, "totals": totals}


def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "ar"
	context.title = _("Aged Receivables")

	company = get_selected_company()
	context.company = company

	as_of = frappe.form_dict.get("as_of") or nowdate()
	context.as_of = str(getdate(as_of))
	context.today = nowdate()

	context.buckets = BUCKETS
	data = _build_aging(company, context.as_of) if company else {"invoices": [], "customers": [], "totals": {}}
	context.invoices = data["invoices"]
	context.customers = data["customers"]
	context.totals = data["totals"]
	return context


@frappe.whitelist()
def download_excel():
	ensure_logged_in()

	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	as_of = frappe.form_dict.get("as_of") or nowdate()
	as_of = str(getdate(as_of))
	data = _build_aging(company, as_of) if company else {"invoices": [], "totals": {}}

	header = [
		_("Customer"),
		_("Invoice"),
		_("Posting Date"),
		_("Due Date"),
		_("Days Past Due"),
		_("Aging Bucket"),
		_("Grand Total"),
		_("Outstanding"),
	]
	out = [header]
	for row in data["invoices"]:
		out.append(
			[
				row["customer"],
				row["name"],
				row["posting_date"],
				row["due_date"],
				row["days_past_due"],
				row["bucket_label"],
				row["grand_total"],
				row["outstanding_amount"],
			]
		)
	if data["invoices"]:
		out.append([_("Total"), "", "", "", "", "", "", data["totals"].get("total", 0)])

	build_xlsx_response(out, f"Aged Receivables {company} as of {as_of}")

# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

"""Every Payment Entry for the selected company, each carrying its own
permanent, per-company sequence number — <abbr>-00001, <abbr>-00002, ... —
assigned once on submit (see ez_accounting.api.assign_payment_sequence_number,
hooked on Payment Entry's on_submit) and backfilled onto every
already-submitted-or-cancelled entry by
ez_accounting.patches.v1_0.backfill_payment_entry_sequence_numbers. A cancelled
entry keeps its number (shown, marked Cancelled) rather than losing it —
same "the number stays even when voided" rule Check Register already
follows for check numbers.

The sequence order follows `creation` (when the entry was actually recorded
in this system), not `posting_date` (the transaction's own, often backdated,
date) — this site's own payment history is years of real transactions
entered progressively over a few weeks, so the two frequently disagree; both
dates are shown (Posting Date / Created) so either is visible at a glance.
"""

import frappe
from frappe import _

from ez_accounting.api import apply_permitted_filter, ensure_admin, get_selected_company

no_cache = 1
PAYMENT_TYPES = {"Receive", "Pay", "Internal Transfer"}


def get_context(context):
	ensure_admin()
	context.body_class = "forms-portal-dark"
	context.nav_active = "payments"
	context.title = _("Payments")
	context.company = get_selected_company()
	return context


def _build_payments(
	company: str,
	from_date: str = "",
	to_date: str = "",
	doc_id: str = "",
	owner: str = "",
	search: str = "",
	payment_type: str = "Receive",
) -> list[dict]:
	"""Shared by the on-screen table, the xlsx download, and the printed
	statement (see printerhtml._render_payments()) -- one source of the real
	rows so none of the three can ever drift from what the others show."""
	if not company:
		return []
	payment_type = (payment_type or "").strip()
	if payment_type == "All":
		payment_type = ""
	elif payment_type and payment_type not in PAYMENT_TYPES:
		frappe.throw(_("Invalid payment type."))

	# List-shaped filters (not dict) from the start -- a date *range* needs
	# two independent conditions on the same posting_date field, which a
	# dict filter can't express (a second dict key of the same name would
	# just overwrite the first).
	filters = [["company", "=", company], ["docstatus", "in", [1, 2]]]
	if from_date:
		filters.append(["posting_date", ">=", from_date])
	if to_date:
		filters.append(["posting_date", "<=", to_date])
	if payment_type:
		filters.append(["payment_type", "=", payment_type])
	doc_id = (doc_id or "").strip()
	if doc_id:
		filters.append(["name", "like", "%{0}%".format(doc_id)])
	owner = (owner or "").strip()
	if owner:
		filters.append(["owner", "like", "%{0}%".format(owner)])
	filters = apply_permitted_filter(filters, "Company", "company")

	rows = frappe.get_all(
		"Payment Entry",
		filters=filters,
		fields=[
			"name",
			"owner",
			"custom_sequence_number",
			"payment_type",
			"party_type",
			"party_name",
			"posting_date",
			"creation",
			"paid_amount",
			# Payment Entry has no single plain "currency" field -- paid_amount
			# is denominated in whichever side it actually moved: paid_to for
			# a Receive (money coming into that account), paid_from for a Pay.
			"paid_from_account_currency",
			"paid_to_account_currency",
			"mode_of_payment",
			"reference_no",
			"docstatus",
			"unallocated_amount",
		],
		# Newest-recorded first -- matches the Sequence No's own ordering
		# principle (creation timestamp, not the transaction's own posting
		# date, which is often backdated -- see [[forms-payments-sequence]]),
		# so the on-screen order and the sequence numbers always agree.
		order_by="creation desc",
		# No cap -- this is a per-company register meant to be complete (the
		# whole point of showing a sequence number is to be able to account
		# for every one of them), not a "recent activity" feed. A 200-row cap
		# here silently dropped a company's oldest entries out of the
		# unfiltered view once it passed 200 total, which is exactly why
		# EIU's own list stopped at EIU-00005 instead of EIU-00001 the first
		# time this shipped -- see [[forms-payments-sequence]].
		limit_page_length=0,
	)
	for r in rows:
		from_currency = r.pop("paid_from_account_currency", None)
		to_currency = r.pop("paid_to_account_currency", None)
		primary = to_currency if r.get("payment_type") == "Receive" else from_currency
		r["currency"] = primary or from_currency or to_currency

	search = (search or "").strip().lower()
	if search:
		rows = [
			r
			for r in rows
			if search in (r.custom_sequence_number or "").lower()
			or search in (r.name or "").lower()
			or search in (r.party_name or "").lower()
			or search in (r.reference_no or "").lower()
		]

	for r in rows:
		r["status"] = "Cancelled" if r.docstatus == 2 else "Submitted"

	return rows


@frappe.whitelist()
def get_payments(from_date: str = "", to_date: str = "", doc_id: str = "", owner: str = "", search: str = "", payment_type: str = "Receive"):
	ensure_admin()
	return _build_payments(get_selected_company(), from_date, to_date, doc_id, owner, search, payment_type)


@frappe.whitelist()
def download_excel(from_date: str = "", to_date: str = "", doc_id: str = "", owner: str = "", search: str = "", payment_type: str = "Receive"):
	ensure_admin()

	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	rows = _build_payments(company, from_date, to_date, doc_id, owner, search, payment_type)

	header = [
		_("Doc ID"),
		_("Owner"),
		_("Sequence No"),
		_("Posting Date"),
		_("Created"),
		_("Type"),
		_("Party"),
		_("Amount"),
		_("Unallocated"),
		_("Mode of Payment"),
		_("Reference No"),
		_("Status"),
	]
	out = [header]
	for r in rows:
		out.append(
			[
				r["name"],
				r["owner"],
				r["custom_sequence_number"] or "",
				frappe.utils.format_date(r["posting_date"]),
				frappe.utils.format_datetime(r["creation"], "M/d/yyyy h:mm a"),
				r["payment_type"] or "",
				r["party_name"] or r["party_type"] or "",
				r["paid_amount"],
				r["unallocated_amount"] or 0,
				r["mode_of_payment"] or "",
				r["reference_no"] or "",
				r["status"],
			]
		)

	build_xlsx_response(out, "Payments - {0}".format(company or "All"))

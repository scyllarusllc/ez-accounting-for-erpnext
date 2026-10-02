# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

"""Every Forms Bank Deposit Sheet for the selected company, each carrying its
own permanent, per-company sequence number — <abbr>-DP-00001, <abbr>-DP-00002,
... — assigned once on submit (see ez_accounting.api.assign_deposit_sequence_number,
hooked on this doctype's on_submit) and backfilled onto every
already-submitted-or-cancelled sheet by
ez_accounting.patches.v1_0.backfill_deposit_sheet_sequence_numbers. A cancelled sheet
keeps its number (shown, marked Cancelled) rather than losing it — same rule
Payment Entry's own sequence follows (see /forms/user/payments,
[[forms-payments-sequence]]).

The sequence order follows `creation` (when the sheet was actually recorded
in this system), not `deposit_date` (which can be backdated) — same
reasoning as the Payments page.
"""

import frappe
from frappe import _

from ez_accounting.api import apply_permitted_filter, ensure_admin, get_selected_company

no_cache = 1


def get_context(context):
	ensure_admin()
	context.body_class = "forms-portal-dark"
	context.nav_active = "deposits"
	context.title = _("Deposits")
	context.company = get_selected_company()
	return context


def _build_deposits(company: str, from_date: str = "", to_date: str = "", search: str = "") -> list[dict]:
	"""Shared by the on-screen table, the xlsx download, and the printed
	statement (see printerhtml._render_deposits()) -- one source of the real
	rows so none of the three can ever drift from what the others show."""
	if not company:
		return []

	filters = [["company", "=", company], ["docstatus", "in", [1, 2]]]
	if from_date:
		filters.append(["deposit_date", ">=", from_date])
	if to_date:
		filters.append(["deposit_date", "<=", to_date])
	filters = apply_permitted_filter(filters, "Company", "company")

	rows = frappe.get_all(
		"Forms Bank Deposit Sheet",
		filters=filters,
		fields=[
			"name",
			"custom_sequence_number",
			"deposit_date",
			"creation",
			"owner",
			"total_deposit",
			"variance",
			"check_variance",
			"docstatus",
		],
		order_by="creation desc",
		limit_page_length=0,
	)
	owners = {row.owner for row in rows if row.owner}
	full_names = {
		user.name: user.full_name
		for user in frappe.get_all("User", filters={"name": ["in", list(owners)]}, fields=["name", "full_name"])
	} if owners else {}

	currency = frappe.get_cached_value("Company", company, "default_currency")
	search = (search or "").strip().lower()
	if search:
		rows = [
			r
			for r in rows
			if search in (r.custom_sequence_number or "").lower() or search in (r.name or "").lower()
		]

	for r in rows:
		r["currency"] = currency
		r["created_by"] = full_names.get(r.owner) or r.owner or ""
		r["status"] = "Cancelled" if r.docstatus == 2 else "Submitted"

	return rows


@frappe.whitelist()
def get_deposits(from_date: str = "", to_date: str = "", search: str = ""):
	ensure_admin()
	return _build_deposits(get_selected_company(), from_date, to_date, search)


@frappe.whitelist()
def download_excel(from_date: str = "", to_date: str = "", search: str = ""):
	ensure_admin()

	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	rows = _build_deposits(company, from_date, to_date, search)

	header = [
		_("Sequence No"),
		_("Deposit Date"),
		_("Created"),
		_("Created By"),
		_("Total Deposit"),
		_("Variance"),
		_("Check Variance"),
		_("Status"),
	]
	out = [header]
	for r in rows:
		out.append(
			[
				r["custom_sequence_number"] or "",
				frappe.utils.format_date(r["deposit_date"]),
				frappe.utils.format_datetime(r["creation"], "M/d/yyyy h:mm a"),
				r["created_by"],
				r["total_deposit"],
				r["variance"] or 0,
				r["check_variance"] or 0,
				r["status"],
			]
		)

	build_xlsx_response(out, "Deposits - {0}".format(company or "All"))

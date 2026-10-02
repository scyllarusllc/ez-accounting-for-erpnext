# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt

from ez_accounting.api import apply_permitted_filter, ensure_admin, get_selected_company

no_cache = 1


def get_context(context):
	ensure_admin()
	context.body_class = "forms-portal-dark"
	context.nav_active = "salesreport"
	context.title = _("Sales Report")
	context.company = get_selected_company()
	return context


def _build_sales_report(company: str, from_date: str = "", to_date: str = "", search: str = "", deposit_day: str = "") -> list[dict]:
	"""Submitted Sales Invoices only, shared by screen, download, and print."""
	if not company:
		return []

	filters = [["company", "=", company], ["docstatus", "=", 1]]
	# The regular From/To range is the invoice posting-date range. Deposit Day
	# is an explicit, separate filter so an old receipt is not pulled into an
	# otherwise posting-date-based search merely because it was deposited later.
	if deposit_day:
		eligible_names = frappe.db.sql_list(
			"""
			SELECT DISTINCT per.reference_name
			FROM `tabPayment Entry Reference` per
			INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent AND pe.docstatus = 1
			INNER JOIN `tabForms Bank Deposit Sheet` bds
				ON bds.name = pe.custom_deposit_sheet AND bds.docstatus = 1
			INNER JOIN `tabSales Invoice` si
				ON si.name = per.reference_name AND si.docstatus = 1 AND si.company = %s
			WHERE per.reference_doctype = 'Sales Invoice' AND per.docstatus = 1
				AND bds.deposit_date = %s
			""",
			(company, deposit_day),
		)
		if not eligible_names:
			return []
		filters.append(["name", "in", eligible_names])
	else:
		if from_date:
			filters.append(["posting_date", ">=", from_date])
		if to_date:
			filters.append(["posting_date", "<=", to_date])
	filters = apply_permitted_filter(filters, "Company", "company")

	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=[
			"name",
			"posting_date",
			"customer",
			"customer_name",
			"grand_total",
			"outstanding_amount",
			"currency",
			"custom_unique_id",
		],
		order_by="posting_date desc, name desc",
		limit_page_length=0,
	)

	invoice_names = [row.name for row in rows]
	modes_by_invoice: dict[str, set[str]] = {}
	sequences_by_invoice: dict[str, set[str]] = {}
	amounts_by_invoice: dict[str, dict[str, float]] = {}
	deposit_dates_by_invoice: dict[str, set] = {}
	deposit_sheets_by_invoice: dict[str, set[str]] = {}
	payment_entries_by_invoice: dict[str, set[str]] = {}
	deposited_entries_by_invoice: dict[str, set[str]] = {}
	deposit_amounts_by_invoice: dict[str, float] = {}
	if invoice_names:
		references = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_doctype": "Sales Invoice", "reference_name": ["in", invoice_names], "docstatus": 1},
			fields=["reference_name", "parent", "allocated_amount"],
			limit_page_length=0,
		)
		payment_names = list({ref.parent for ref in references})
		payment_by_name = {}
		if payment_names:
			payment_by_name = {
				payment.name: payment
				for payment in frappe.get_all(
					"Payment Entry",
					filters={"name": ["in", payment_names], "docstatus": 1},
					fields=["name", "mode_of_payment", "custom_sequence_number", "custom_deposit_sheet"],
					limit_page_length=0,
				)
			}
		for ref in references:
			if payment := payment_by_name.get(ref.parent):
				payment_entries_by_invoice.setdefault(ref.reference_name, set()).add(payment.name)
				if payment.mode_of_payment:
					modes_by_invoice.setdefault(ref.reference_name, set()).add(payment.mode_of_payment)
				sequences_by_invoice.setdefault(ref.reference_name, set()).add(payment.custom_sequence_number or payment.name)
				bucket = _payment_bucket(payment.mode_of_payment)
				amounts = amounts_by_invoice.setdefault(ref.reference_name, {})
				amounts[bucket] = amounts.get(bucket, 0) + flt(ref.allocated_amount)
				if payment.custom_deposit_sheet:
					deposit = frappe.db.get_value(
						"Forms Bank Deposit Sheet", payment.custom_deposit_sheet,
						["name", "deposit_date", "docstatus"], as_dict=True,
					)
					if deposit and deposit.docstatus == 1:
						deposited_entries_by_invoice.setdefault(ref.reference_name, set()).add(payment.name)
						deposit_sheets_by_invoice.setdefault(ref.reference_name, set()).add(deposit.name)
						if deposit.deposit_date:
							deposit_dates_by_invoice.setdefault(ref.reference_name, set()).add(deposit.deposit_date)
							if not deposit_day or str(deposit.deposit_date) == str(deposit_day):
								deposit_amounts_by_invoice[ref.reference_name] = deposit_amounts_by_invoice.get(ref.reference_name, 0) + flt(ref.allocated_amount)

		# POS invoices can store modes directly without a separate Payment Entry.
		for payment in frappe.get_all(
			"Sales Invoice Payment",
			filters={"parent": ["in", invoice_names], "docstatus": 1},
			fields=["parent", "mode_of_payment", "amount"],
			limit_page_length=0,
		):
			if payment.mode_of_payment:
				modes_by_invoice.setdefault(payment.parent, set()).add(payment.mode_of_payment)
			bucket = _payment_bucket(payment.mode_of_payment)
			amounts = amounts_by_invoice.setdefault(payment.parent, {})
			amounts[bucket] = amounts.get(bucket, 0) + flt(payment.amount)

	for row in rows:
		row["paid_amount"] = max(flt(row.grand_total) - flt(row.outstanding_amount), 0)
		row["mode_of_payment"] = ", ".join(sorted(modes_by_invoice.get(row.name, set())))
		row["receipt_no"] = ", ".join(sorted(sequences_by_invoice.get(row.name, set())))
		row["sequence_id"] = row["receipt_no"]
		amounts = amounts_by_invoice.get(row.name, {})
		row["credit_card_amount"] = amounts.get("credit_card", 0)
		row["check_cash_amount"] = amounts.get("check_cash", 0)
		row["wire_transfer_amount"] = amounts.get("wire_transfer", 0)
		row["other_payment_amount"] = amounts.get("other", 0)
		payment_entries = payment_entries_by_invoice.get(row.name, set())
		deposited_entries = deposited_entries_by_invoice.get(row.name, set())
		deposit_dates = deposit_dates_by_invoice.get(row.name, set())
		row["deposit_sheets"] = ", ".join(sorted(deposit_sheets_by_invoice.get(row.name, set())))
		row["deposit_amount"] = deposit_amounts_by_invoice.get(row.name, 0)
		row["deposit_day"] = max(deposit_dates) if deposit_dates else None
		# Credit-card payments are settled outside the bank-deposit workflow, so
		# a deposit status would be misleading for those sales.
		if any(_payment_bucket(mode) == "credit_card" for mode in modes_by_invoice.get(row.name, set())):
			row["deposit_status"] = "—"
			row["closed_date"] = None
		elif payment_entries and deposited_entries == payment_entries:
			row["deposit_status"] = "Closed"
			row["closed_date"] = max(deposit_dates) if deposit_dates else None
		elif deposited_entries:
			row["deposit_status"] = "Partially Deposited"
			row["closed_date"] = None
		else:
			row["deposit_status"] = "Undeposited"
			row["closed_date"] = None
	search = (search or "").strip().lower()
	if search:
		rows = [r for r in rows if any(search in str(value or "").lower() for value in
			(r.name, r.customer, r.customer_name, r.custom_unique_id, r.receipt_no,
			 r.deposit_sheets, r.deposit_status, r.deposit_day))]
	return rows


def _payment_bucket(mode: str | None) -> str:
	mode = (mode or "").strip().lower()
	if mode == "credit card":
		return "credit_card"
	if mode.replace(" ", "").replace("-", "") == "wiretransfer":
		return "wire_transfer"
	if mode.startswith("check") or mode.startswith("cash"):
		return "check_cash"
	return "other"


def _sales_summary(rows: list[dict]) -> dict:
	return {
		"invoice_count": len(rows),
		"total_amount": sum(flt(row.grand_total) for row in rows),
		"total_paid": sum(flt(row.paid_amount) for row in rows),
		"total_credit_card": sum(flt(row.credit_card_amount) for row in rows),
		"total_check_cash": sum(flt(row.check_cash_amount) for row in rows),
		"total_wire_transfer": sum(flt(row.wire_transfer_amount) for row in rows),
		"total_other_payment": sum(flt(row.other_payment_amount) for row in rows),
		"total_deposit_amount": sum(flt(row.deposit_amount) for row in rows),
		"currency": rows[0].currency if rows else "",
	}


@frappe.whitelist()
def get_sales(from_date: str = "", to_date: str = "", search: str = "", deposit_day: str = ""):
	ensure_admin()
	rows = _build_sales_report(get_selected_company(), from_date, to_date, search, deposit_day)
	return {"rows": rows, "summary": _sales_summary(rows)}


@frappe.whitelist()
def download_excel(from_date: str = "", to_date: str = "", search: str = "", deposit_day: str = ""):
	ensure_admin()
	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	rows = _build_sales_report(company, from_date, to_date, search, deposit_day)
	out = [[_("Receipt #"), _("Posting Date"), _("Customer"), _("Deposit Day"), _("Deposit Status"), _("Deposit Amount"), _("Credit Card"), _("Check / Cash"), _("Wire Transfer"), _("Other Payment"), _("Amount"), _("Paid")]]
	for row in rows:
		out.append([
			row.receipt_no or "—",
			frappe.utils.format_date(row.posting_date),
			row.customer_name or row.customer,
			frappe.utils.format_date(row.deposit_day) if row.deposit_day else "",
			_(row.deposit_status),
			row.deposit_amount,
			row.credit_card_amount,
			row.check_cash_amount,
			row.wire_transfer_amount,
			row.other_payment_amount,
			row.grand_total,
			row.paid_amount,
		])
	summary = _sales_summary(rows)
	out.extend([[], [_("Summary"), _("Receipts"), summary["invoice_count"], "", "", summary["total_deposit_amount"], summary["total_credit_card"], summary["total_check_cash"], summary["total_wire_transfer"], summary["total_other_payment"], summary["total_amount"], summary["total_paid"]]])
	build_xlsx_response(out, "Sales Report - {0}".format(company or "All"))

# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import csv
import io

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

# ERPNext's own helper for turning a Purchase Invoice into a matching Payment
# Entry — same reasoning as purchase.py's own process_express_purchase().
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

from ez_accounting.api import ensure_admin, get_selected_company

# Reused rather than duplicated — same "Mode of Payment's own account, else
# the company's default bank account" resolution the live Express Purchase
# page uses, so an imported purchase can never resolve a different account
# than typing the same row in by hand would.
from ez_accounting.www.forms.user.purchase import _resolve_paid_from_account

no_cache = 1

REQUIRED_COLUMNS = {"supplier", "item", "qty", "rate"}
OPTIONAL_COLUMNS = {"posting_date", "mode_of_payment", "reference_no", "remarks"}

CSV_TEMPLATE = (
	"supplier,item,qty,rate,posting_date,mode_of_payment,reference_no,remarks\n"
	"Chuck Kurpf,item1,2,50,2026-08-01,,,Sample import row\n"
)


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "purchase"
	context.title = _("Import Purchases")
	context.company = get_selected_company()
	context.csv_template = CSV_TEMPLATE
	return context


def _parse_import_rows(csv_text: str) -> list[dict]:
	"""Parse+validate every row of pasted CSV, one Purchase Invoice+Payment
	Entry per row (multi-item invoices aren't supported by this import — one
	row is one complete single-line purchase, same shape Express Purchase had
	before multi-item support). Shared by preview_import() and
	import_purchases() so what the admin previewed is exactly what submitting
	re-checks — never two different code paths that could quietly drift apart.
	Unlike process_express_purchase() (always nowdate()), posting_date is a
	real column here — the whole point of a bulk import is backdating
	historical purchases, so defaulting every row to today would defeat it.
	"""
	csv_text = (csv_text or "").strip()
	if not csv_text:
		frappe.throw(_("Paste some CSV first."))

	reader = csv.DictReader(io.StringIO(csv_text))
	if not reader.fieldnames:
		frappe.throw(_("Could not read a header row from the CSV."))

	headers = {(h or "").strip().lower() for h in reader.fieldnames}
	missing = REQUIRED_COLUMNS - headers
	if missing:
		frappe.throw(_("Missing required column(s): {0}").format(", ".join(sorted(missing))))

	rows = []
	for line_no, raw_row in enumerate(reader, start=2):  # header is line 1
		row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items() if k}
		if not any(row.values()):
			continue  # blank line — not a row, not an error either

		errors = []

		supplier_input = row.get("supplier", "")
		supplier_doc = None
		if not supplier_input:
			errors.append(_("Supplier is required."))
		else:
			supplier_doc = frappe.db.get_value(
				"Supplier", supplier_input, ["name", "supplier_name", "disabled"], as_dict=True
			)
			if not supplier_doc:
				errors.append(_("Supplier {0} not found.").format(supplier_input))
			elif supplier_doc.disabled:
				errors.append(_("Supplier {0} is disabled.").format(supplier_input))

		item_input = row.get("item", "")
		item_doc = None
		if not item_input:
			errors.append(_("Item is required."))
		else:
			item_doc = frappe.db.get_value(
				"Item", item_input, ["name", "item_name", "disabled", "is_purchase_item"], as_dict=True
			)
			if not item_doc:
				errors.append(_("Item {0} not found.").format(item_input))
			elif item_doc.disabled:
				errors.append(_("Item {0} is disabled.").format(item_input))
			elif not item_doc.is_purchase_item:
				errors.append(_("Item {0} is not a purchase item.").format(item_input))

		qty_raw = row.get("qty", "")
		qty = flt(qty_raw) if qty_raw else 0
		if qty <= 0:
			errors.append(_("Quantity must be greater than zero."))

		rate_raw = row.get("rate", "")
		rate = flt(rate_raw) if rate_raw else 0
		if rate <= 0:
			errors.append(_("Rate must be greater than zero."))

		posting_date_raw = row.get("posting_date", "")
		posting_date = None
		if posting_date_raw:
			try:
				posting_date = getdate(posting_date_raw)
			except Exception:
				errors.append(_("Invalid posting date {0} — use YYYY-MM-DD.").format(posting_date_raw))
		else:
			posting_date = getdate(nowdate())

		mode_of_payment = row.get("mode_of_payment", "")
		if mode_of_payment and not frappe.db.exists(
			"Mode of Payment", {"name": mode_of_payment, "enabled": 1}
		):
			errors.append(_("{0} is not an active Mode of Payment.").format(mode_of_payment))

		rows.append(
			{
				"row": line_no,
				"supplier": supplier_doc.name if supplier_doc else supplier_input,
				"supplier_name": supplier_doc.supplier_name if supplier_doc else None,
				"item": item_doc.name if item_doc else item_input,
				"item_name": item_doc.item_name if item_doc else None,
				"qty": qty,
				"rate": rate,
				"amount": qty * rate,
				"posting_date": str(posting_date) if posting_date else None,
				"mode_of_payment": mode_of_payment or None,
				"reference_no": row.get("reference_no") or None,
				"remarks": row.get("remarks") or None,
				"errors": errors,
				"valid": not errors,
			}
		)

	if not rows:
		frappe.throw(_("No data rows found — only a header row (or blank lines)."))

	return rows


@frappe.whitelist()
def preview_import(csv_text: str):
	"""Parse+validate only — nothing is created. The admin reviews this table
	before import_purchases() re-parses the same text and actually acts on it.
	"""
	ensure_admin()
	rows = _parse_import_rows(csv_text)
	return {
		"rows": rows,
		"valid_count": sum(1 for r in rows if r["valid"]),
		"invalid_count": sum(1 for r in rows if not r["valid"]),
	}


@frappe.whitelist()
def import_purchases(csv_text: str):
	"""Re-parses+re-validates csv_text (server truth can't be trusted to
	still match what preview_import() showed a moment ago — a Supplier/Item/
	Mode of Payment could have been disabled in between), then creates one
	Purchase Invoice + Payment Entry per valid row, each in its own
	insert/submit/commit — one bad row (e.g. a Payment Entry validation
	quirk preview_import() couldn't have predicted) rolls back only itself
	and is reported as an error, not aborting the whole batch. Invalid rows
	from parsing are skipped outright, also reported, so every single line
	the admin pasted gets an explicit outcome.
	"""
	ensure_admin()
	rows = _parse_import_rows(csv_text)

	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	results = []
	for row in rows:
		if not row["valid"]:
			results.append(dict(row, status="skipped"))
			continue

		try:
			paid_from_account = _resolve_paid_from_account(company, row["mode_of_payment"] or "")
			if not paid_from_account:
				frappe.throw(
					_(
						"Could not resolve an account to pay from for {0}. Set a default bank account"
						" for the company."
					).format(company)
				)

			pi = frappe.get_doc(
				{
					"doctype": "Purchase Invoice",
					"supplier": row["supplier"],
					"company": company,
					"posting_date": row["posting_date"],
					"set_posting_time": 1,
					"due_date": row["posting_date"],
					"remarks": row["remarks"],
					"items": [{"item_code": row["item"], "qty": row["qty"], "rate": row["rate"]}],
				}
			)
			pi.insert()
			pi.submit()

			pe = get_payment_entry("Purchase Invoice", pi.name, bank_account=paid_from_account)
			pe.posting_date = row["posting_date"]
			if row["mode_of_payment"]:
				pe.mode_of_payment = row["mode_of_payment"]
			pe.reference_no = row["reference_no"] or pi.name
			pe.reference_date = row["posting_date"]
			if row["remarks"]:
				pe.remarks = row["remarks"]
				pe.custom_remarks = 1
				pe.custom_line_descriptions = row["remarks"]
			pe.insert()
			pe.submit()

			frappe.db.commit()
			results.append(
				dict(row, status="created", purchase_invoice=pi.name, payment_entry=pe.name)
			)
		except Exception as e:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), "Purchase Import row failed")
			# The full traceback goes to the Error Log (above); only the
			# exception's own message is returned to the client — same
			# reasoning as every other page's extract_error_message() never
			# showing raw server internals, just what actually needs fixing.
			results.append(dict(row, status="error", error=str(e)))

	return {
		"results": results,
		"created_count": sum(1 for r in results if r["status"] == "created"),
		"error_count": sum(1 for r in results if r["status"] == "error"),
		"skipped_count": sum(1 for r in results if r["status"] == "skipped"),
	}

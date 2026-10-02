# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate

from ez_accounting.api import apply_permitted_filter, ensure_admin, get_selected_company

no_cache = 1

THRESHOLD = 600.0

# 1099 box -> short description, per form. Box "1" defaults for NEC; MISC has
# the fuller set. Used for the "Box Desc" column.
BOX_DESC = {
	"NEC": {
		"1": "Nonemployee compensation",
		"4": "Federal income tax withheld",
	},
	"MISC": {
		"1": "Rents",
		"2": "Royalties",
		"3": "Other income",
		"4": "Federal income tax withheld",
		"5": "Fishing boat proceeds",
		"6": "Medical and health care payments",
		"7": "Direct sales of $5,000 or more",
		"8": "Substitute payments in lieu of dividends or interest",
		"9": "Crop insurance proceeds",
		"10": "Gross proceeds paid to an attorney",
		"14": "Nonqualified deferred compensation",
	},
}
DEFAULT_TYPE = "NEC"
DEFAULT_BOX = "1"


def _year() -> str:
	year = (frappe.form_dict.get("year") or "").strip()
	if year.isdigit() and len(year) == 4:
		return year
	return nowdate()[:4]


def _box_desc(kind: str, box: str) -> str:
	return BOX_DESC.get(kind or DEFAULT_TYPE, {}).get(box or DEFAULT_BOX, "")


def _supplier_payments(company: str, year: str, suppliers: list[str]) -> dict[str, list]:
	"""Every submitted Pay-type Payment Entry to each supplier in `year` for
	`company`, grouped by party. The transactions a 1099 is built from."""
	if not suppliers:
		return {}
	rows = frappe.get_all(
		"Payment Entry",
		filters=[
			["payment_type", "=", "Pay"],
			["party_type", "=", "Supplier"],
			["party", "in", suppliers],
			["company", "=", company],
			["docstatus", "=", 1],
			["posting_date", "between", [f"{year}-01-01", f"{year}-12-31"]],
		],
		fields=["party", "name", "posting_date", "reference_no", "paid_amount"],
		order_by="party asc, posting_date asc, name asc",
	)
	out: dict[str, list] = {}
	for r in rows:
		out.setdefault(r.party, []).append(r)
	return out


def _build_report(company: str, year: str) -> dict:
	filters = apply_permitted_filter([["irs_1099", "=", 1]], "Supplier", "name")
	flagged = frappe.get_all(
		"Supplier",
		filters=filters,
		fields=["name", "supplier_name", "tax_id", "custom_1099_type", "custom_1099_box"],
		order_by="supplier_name asc",
	)
	if not company or not flagged:
		return {"vendors": [], "total": 0.0, "met_count": 0, "no_activity": 0}

	pay_by_party = _supplier_payments(company, year, [s.name for s in flagged])

	vendors = []
	total = 0.0
	met_count = 0
	no_activity = 0
	for s in flagged:
		txns = pay_by_party.get(s.name, [])
		if not txns:
			no_activity += 1
			continue
		kind = s.custom_1099_type or DEFAULT_TYPE
		box = s.custom_1099_box or DEFAULT_BOX
		subtotal = sum(flt(t.paid_amount) for t in txns)
		limit_met = subtotal >= THRESHOLD
		if limit_met:
			met_count += 1
		total += subtotal
		vendors.append(
			{
				"vendor_id": s.name,
				"vendor": s.supplier_name or s.name,
				"tax_id": s.tax_id or "",
				"kind": kind,
				"box": box,
				"box_desc": _box_desc(kind, box),
				"limit_met": limit_met,
				"subtotal": subtotal,
				"rows": [
					{
						"date": t.posting_date,
						"trans_no": t.reference_no or t.name,
						"amount": flt(t.paid_amount),
					}
					for t in txns
				],
			}
		)
	return {"vendors": vendors, "total": total, "met_count": met_count, "no_activity": no_activity}


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "vendor_1099"
	context.title = _("1099 Vendor Report")

	company = get_selected_company()
	context.company = company
	context.year = _year()
	context.current_year = nowdate()[:4]
	context.threshold = THRESHOLD

	data = _build_report(company, context.year)
	context.vendors = data["vendors"]
	context.report_total = data["total"]
	context.met_count = data["met_count"]
	context.no_activity = data["no_activity"]
	return context


# ---- 1099 setup (which suppliers are 1099, and their type/box) ----

BOX_OPTIONS = {kind: sorted(boxes, key=lambda b: int(b)) for kind, boxes in BOX_DESC.items()}


@frappe.whitelist()
def get_setup(year: str = ""):
	"""For the setup panel: the currently-flagged 1099 suppliers (with their
	paid total for the year) plus "candidates" — suppliers paid >= the
	threshold this year that aren't flagged yet."""
	ensure_admin()
	company = get_selected_company()
	year = year if (year.isdigit() and len(year) == 4) else nowdate()[:4]

	flagged = frappe.get_all(
		"Supplier",
		filters=apply_permitted_filter([["irs_1099", "=", 1]], "Supplier", "name"),
		fields=["name", "supplier_name", "custom_1099_type", "custom_1099_box"],
		order_by="supplier_name asc",
	)
	paid = _paid_by_supplier(company, year)
	for s in flagged:
		s["paid"] = paid.get(s.name, 0.0)

	flagged_names = {s.name for s in flagged}
	candidates = [
		{"name": name, "supplier_name": frappe.db.get_value("Supplier", name, "supplier_name") or name, "paid": amount}
		for name, amount in sorted(paid.items(), key=lambda kv: kv[1], reverse=True)
		if name not in flagged_names and amount >= THRESHOLD
	][:50]

	return {
		"flagged": flagged,
		"candidates": candidates,
		"box_options": BOX_OPTIONS,
		"box_desc": BOX_DESC,
	}


def _paid_by_supplier(company: str, year: str) -> dict[str, float]:
	if not company:
		return {}
	rows = frappe.get_all(
		"Payment Entry",
		filters=[
			["payment_type", "=", "Pay"],
			["party_type", "=", "Supplier"],
			["company", "=", company],
			["docstatus", "=", 1],
			["posting_date", "between", [f"{year}-01-01", f"{year}-12-31"]],
		],
		fields=["party", "paid_amount"],
	)
	out: dict[str, float] = {}
	for r in rows:
		out[r.party] = out.get(r.party, 0.0) + flt(r.paid_amount)
	return out


@frappe.whitelist()
def search_suppliers(q: str = ""):
	ensure_admin()
	q = (q or "").strip()
	filters = [["disabled", "=", 0]]
	if q:
		filters.append(["supplier_name", "like", f"%{q}%"])
	filters = apply_permitted_filter(filters, "Supplier", "name")
	return frappe.get_all(
		"Supplier",
		filters=filters,
		fields=["name", "supplier_name", "irs_1099", "custom_1099_type", "custom_1099_box"],
		order_by="supplier_name asc",
		limit_page_length=40,
	)


@frappe.whitelist()
def set_supplier_1099(supplier: str, irs_1099="0", kind: str = "", box: str = ""):
	ensure_admin()
	if not frappe.db.exists("Supplier", supplier):
		frappe.throw(_("{0} is not a supplier.").format(supplier))
	# a permission-restricted user can only touch suppliers they may see
	permitted = apply_permitted_filter([["name", "=", supplier]], "Supplier", "name")
	if not frappe.get_all("Supplier", filters=permitted, limit_page_length=1):
		frappe.throw(_("Not permitted."), frappe.PermissionError)

	kind = (kind or "").strip().upper()
	if kind and kind not in BOX_DESC:
		frappe.throw(_("1099 Type must be NEC or MISC."))
	box = (box or "").strip()

	doc = frappe.get_doc("Supplier", supplier)
	doc.irs_1099 = 1 if cint(irs_1099) else 0
	doc.custom_1099_type = kind or None
	doc.custom_1099_box = box or None
	doc.save(ignore_permissions=True)
	return {
		"name": doc.name,
		"irs_1099": doc.irs_1099,
		"custom_1099_type": doc.custom_1099_type or "",
		"custom_1099_box": doc.custom_1099_box or "",
	}


@frappe.whitelist()
def download_excel():
	ensure_admin()
	from frappe.utils.xlsxutils import build_xlsx_response

	company = get_selected_company()
	year = _year()
	data = _build_report(company, year)

	header = [
		_("Vendor ID"),
		_("Vendor"),
		_("Tax ID"),
		_("1099 Type"),
		_("1099 Box"),
		_("Box Desc"),
		_("Date"),
		_("Trans No"),
		_("Trans Amount"),
		_("Limit Met?"),
	]
	out = [header]
	for v in data["vendors"]:
		for i, row in enumerate(v["rows"]):
			out.append(
				[
					v["vendor_id"] if i == 0 else "",
					v["vendor"] if i == 0 else "",
					v["tax_id"] if i == 0 else "",
					v["kind"] if i == 0 else "",
					v["box"] if i == 0 else "",
					v["box_desc"] if i == 0 else "",
					row["date"],
					row["trans_no"],
					row["amount"],
					"",
				]
			)
		out.append(["", "", "", "", "", "", "", _("Subtotal"), v["subtotal"], _("Yes") if v["limit_met"] else _("No")])
	if data["vendors"]:
		out.append(["", _("Report Total"), "", "", "", "", "", "", data["total"], ""])

	build_xlsx_response(out, f"1099 Vendor Report {company} {year}")

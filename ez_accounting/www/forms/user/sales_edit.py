# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _

from ez_accounting.api import ensure_admin, ensure_logged_in, get_selected_company, permitted_docs

no_cache = 1


def get_context(context):
	"""Reached only via the "Edit" link on a Sales Invoice History row (see
	sales.html) -- no nav link of its own, same "detail page with no picker,
	only reachable from the list" shape student_customer_profile.html already
	established. `?id=<sales_invoice>` in the URL, same query-param
	convention /forms/user/printer already uses.

	Despite the page's own name/URL, this isn't an editor for the invoice's
	real fields at all -- see get_sales_invoice_attachments()/
	upload_sales_invoice_attachment()/delete_sales_invoice_attachment() and
	save_sales_invoice_enhanced_remarks() below. An earlier version of this
	page let the admin set 3 "correction note" fields (Posting Date/
	Reference No/Remarks) that deliberately never touched the invoice's own
	real data; that was removed outright (Custom Fields deleted, confirmed
	zero real data existed in any of them first) in favor of real file
	attachments (and, later, Enhanced Remarks -- a similar "additional field,
	not the invoice's own data" note, just richer) instead, which is what
	"editing" an already-submitted sale actually turned out to mean here.

	Any logged-in portal user (not just the ADMIN_ROLES set) may open this
	page and use it -- non-admin office staff need to be able to attach
	supporting files and keep the Enhanced Remarks note on a sale. Only
	deleting an attachment (delete_sales_invoice_attachment() below) stays
	admin-only.
	"""
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "sales_edit"
	context.title = _("Edit Sale")
	context.company = get_selected_company()
	context.sales_invoice = frappe.form_dict.get("id") or ""
	return context


def _get_invoice(name: str, check_company: bool = True):
	if not frappe.db.exists("Sales Invoice", name):
		frappe.throw(_("{0} is not a valid Sales Invoice.").format(name))

	doc = frappe.get_doc("Sales Invoice", name)
	if check_company:
		company = get_selected_company()
		if company and doc.company != company:
			frappe.throw(_("{0} does not belong to {1}.").format(name, company), frappe.PermissionError)
		# Independent of the selected company: a user restricted to certain
		# Companies via User Permission can never open an invoice outside them,
		# even by passing its name directly.
		permitted_companies = permitted_docs("Company")
		if permitted_companies is not None and doc.company not in permitted_companies:
			frappe.throw(_("You are not permitted to view {0}.").format(name), frappe.PermissionError)
	return doc


@frappe.whitelist()
def get_sales_invoice_edit_data(name: str):
	"""The real invoice's own read-only summary (for context -- customer,
	dates, amounts, status, line items) plus its own Payment History (every
	submitted Payment Entry allocated against it, same shape the Statement
	of Account already itemizes -- see printerhtml.py's
	_render_statement_of_account()), shown above the attachments list.
	Line items are included the same way the Statement of Account already
	does (batched Sales Invoice Item query) -- same "show what this invoice
	actually was for" reasoning.
	"""
	ensure_logged_in()
	doc = _get_invoice(name)

	items = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": name},
		fields=["item_code", "item_name", "description", "qty", "rate", "amount"],
		order_by="idx asc",
	)

	# Payment History -- one row per (Payment Entry, invoice) allocation, not
	# one row per Payment Entry, same reasoning as the Statement of Account:
	# a Split Payment sale (see [[forms-express-sales]]) can allocate more
	# than one Payment Entry to a single invoice. docstatus=1 filtered
	# directly on the child row (mirrors the parent's), same shape
	# sales.py's own cancel_express_sale() already uses.
	refs = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": name, "docstatus": 1},
		fields=["parent", "allocated_amount"],
	)
	payments = []
	paid_amount = 0
	if refs:
		pe_names = sorted({r.parent for r in refs})
		pe_by_name = {
			row.name: row
			for row in frappe.get_all(
				"Payment Entry",
				filters={"name": ["in", pe_names]},
				fields=["name", "posting_date", "mode_of_payment", "reference_no"],
			)
		}
		for ref in refs:
			pe = pe_by_name.get(ref.parent)
			if not pe:
				continue
			payments.append(
				{
					"payment_entry": pe.name,
					"posting_date": pe.posting_date,
					"mode_of_payment": pe.mode_of_payment,
					"reference_no": pe.reference_no,
					"amount": ref.allocated_amount,
				}
			)
			paid_amount += ref.allocated_amount
		payments.sort(key=lambda p: (p["posting_date"] or frappe.utils.getdate("1900-01-01")), reverse=True)

	return {
		"name": doc.name,
		"customer": doc.customer,
		"customer_name": doc.customer_name,
		# Flags a Customer in the "Student" group so the page can link to
		# /forms/user/student_customer_profile (same group the Student
		# Customers pages scope by).
		"is_student": frappe.db.get_value("Customer", doc.customer, "customer_group") == "Student",
		"posting_date": doc.posting_date,
		"status": doc.status,
		"grand_total": doc.grand_total,
		"outstanding_amount": doc.outstanding_amount,
		"paid_amount": paid_amount,
		"currency": doc.currency,
		"remarks": doc.remarks,
		"items": items,
		"payments": payments,
		"custom_enhanced_remarks": doc.custom_enhanced_remarks,
	}


@frappe.whitelist()
def save_sales_invoice_enhanced_remarks(name: str, content: str = ""):
	"""Saves "Enhanced Remarks" -- a rich note (plain text plus inline pasted
	images) kept in its own field, separate from the invoice's own plain
	`remarks`, same "additional field in the app, don't touch the invoice's
	real data" reasoning as this page's earlier (now-removed) correction-note
	fields. The image itself is uploaded separately, client-side, straight to
	/api/method/upload_file (same technique as get_sales_invoice_attachments()
	below) -- this only ever receives the resulting HTML (an <img src="...">
	pointing at that already-uploaded file, plus whatever text surrounds it).

	Sanitized server-side via the same frappe.utils.sanitize_html() every
	real Text Editor field on this site already goes through -- the content
	comes from a contenteditable div's own innerHTML, which a browser's paste
	handling can populate with more than just the img tag this feature
	intentionally allows (arbitrary attributes/tags from whatever was
	copied), and it's re-rendered as real HTML for every admin who later
	opens this page, so unsanitized input here would be a stored-XSS risk.
	"""
	ensure_logged_in()
	doc = _get_invoice(name)

	from frappe.utils import sanitize_html

	clean_content = sanitize_html(content or "", always_sanitize=True)

	frappe.db.set_value("Sales Invoice", doc.name, "custom_enhanced_remarks", clean_content, update_modified=True)
	frappe.db.commit()

	return {"name": doc.name, "custom_enhanced_remarks": clean_content}


@frappe.whitelist()
def get_sales_invoice_attachments(name: str):
	"""Every File already attached to this Sales Invoice -- the file itself is
	uploaded through upload_sales_invoice_attachment() below -- this just
	lists what's there.
	"""
	ensure_logged_in()
	_get_invoice(name)

	return frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Sales Invoice", "attached_to_name": name},
		fields=["name", "file_name", "file_url", "creation", "owner"],
		order_by="creation desc",
	)


@frappe.whitelist()
def upload_sales_invoice_attachment(name: str):
	"""Attaches one uploaded file (the attachments file picker, or a
	pasted-from-clipboard image in the Enhanced Remarks editor) to this Sales
	Invoice as a private File.

	This exists instead of the client posting straight to Frappe's own
	/api/method/upload_file (the technique this page used before, and the Add
	Sale form still uses) because that endpoint requires *write* permission
	on the target Sales Invoice, which non-admin portal staff don't have.
	Here the caller only has to be logged in and name a real invoice in the
	currently selected company (_get_invoice()'s own check); the File is then
	created with ignore_permissions, exactly like
	delete_sales_invoice_attachment() removes one.

	The multipart body carries a single "file" part, same shape
	/api/method/upload_file expects, so the client-side upload helper barely
	changes. Content type is checked against an allowlist so this can't be
	used to attach arbitrary executable content: Frappe's own
	ALLOWED_MIMETYPES (PDF, plain text, CSV, Microsoft/OpenDocument), plus
	*any* raster image type. The image allowance is deliberately broader than
	ALLOWED_MIMETYPES' fixed png/jpeg/gif -- a screenshot pasted into
	Enhanced Remarks arrives as whatever the OS/browser put on the clipboard
	(Safari on macOS gives image/tiff, newer Chrome/Windows give image/webp,
	etc.), and rejecting those is exactly the bug this endpoint hit in the
	field. SVG is excluded (it can carry script and File may serve it inline).
	"""
	ensure_logged_in()
	_get_invoice(name)

	files = getattr(frappe.request, "files", None)
	uploaded = files.get("file") if files else None
	if not uploaded:
		frappe.throw(_("No file was uploaded."))

	content = uploaded.stream.read()
	filename = uploaded.filename or "upload"

	from mimetypes import guess_type

	from frappe.handler import ALLOWED_MIMETYPES

	content_type = (getattr(uploaded, "mimetype", None) or guess_type(filename)[0] or "").lower()
	is_allowed = content_type in ALLOWED_MIMETYPES or (
		content_type.startswith("image/") and content_type != "image/svg+xml"
	)
	if not is_allowed:
		frappe.throw(_("You can only upload an image, a PDF, or a text/Office document."))

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"attached_to_doctype": "Sales Invoice",
			"attached_to_name": name,
			"file_name": filename,
			"is_private": 1,
			"content": content,
		}
	).save(ignore_permissions=True)
	frappe.db.commit()

	return {"file_url": file_doc.file_url, "file_name": file_doc.file_name, "name": file_doc.name}


@frappe.whitelist()
def delete_sales_invoice_attachment(name: str, file_name: str):
	"""Removes one attached File -- checked against *this* Sales Invoice's own
	attachments first (not just any File name the caller happens to pass),
	so this can't be used to delete an unrelated file elsewhere on the site.
	"""
	ensure_admin()
	_get_invoice(name)

	file_doc = frappe.get_doc("File", file_name)
	if file_doc.attached_to_doctype != "Sales Invoice" or file_doc.attached_to_name != name:
		frappe.throw(_("{0} is not an attachment on {1}.").format(file_name, name))

	frappe.delete_doc("File", file_name, ignore_permissions=True)
	frappe.db.commit()

	return {"removed": file_name}

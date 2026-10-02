import frappe


FORMAT = "Official Receipt with Descriptions"
CSS_ANCHOR = "\t.lh-frame img {"
CSS_ADD = "\t.lh-frame .invoice-title, .lh-frame .invoice-number { display: none; }\n"
OLD_LH = '{{ frappe.render_template(frappe.db.get_value("Letter Head", "Company Letterhead - Grey", "content"), {"doc": doc}) }}'
NEW_LH = (
	'{% set lh_address = frappe.db.sql("SELECT a.name FROM `tabAddress` a JOIN `tabDynamic Link` d ON d.parent = a.name '
	'WHERE d.link_doctype = \'Company\' AND d.link_name = %s ORDER BY a.is_primary_address DESC, a.creation LIMIT 1", doc.company) %}\n'
	'\t\t{{ frappe.render_template(frappe.db.get_value("Letter Head", "Company Letterhead - Grey", "content"), '
	'{"doc": {"doctype": doc.doctype, "name": doc.name, "company": doc.company, "company_address": lh_address[0][0] if lh_address else None}}) }}'
)


def execute():
	"""Hide the letterhead's duplicate doctype/number block and show the company address on the receipt."""
	if not frappe.db.exists("Print Format", FORMAT):
		return

	html = frappe.db.get_value("Print Format", FORMAT, "html") or ""
	if OLD_LH not in html or "lh-frame .invoice-title" in html:
		return

	html = html.replace(OLD_LH, NEW_LH, 1).replace(CSS_ANCHOR, CSS_ADD + CSS_ANCHOR, 1)
	frappe.db.set_value("Print Format", FORMAT, "html", html)

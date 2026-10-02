import frappe


FORMAT = "Official Receipt with Descriptions"
MARKER = "/* receipt-header-compact */"
CSS_ANCHOR = "\t.lh-frame { margin-bottom: 6px; }\n"
CSS_NEW = (
	"\t.lh-frame { margin-bottom: 18px; }\n"
	"\t" + MARKER + "\n"
	"\t.lh-frame, .lh-frame td, .lh-frame div { font-size: 10.5px !important; line-height: 1.35 !important; }\n"
	"\t.lh-frame .company-name { font-size: 12.5px !important; line-height: 1.3 !important; margin-bottom: 3px !important; }\n"
	"\t.lh-frame .company-address { width: auto !important; }\n"
	"\t.lh-frame .letter-head { padding: 8px 10px !important; margin: 0 !important; }\n"
)


def execute():
	"""Shrink the letterhead text so the company block no longer crowds the OFFICIAL RECEIPT title."""
	if not frappe.db.exists("Print Format", FORMAT):
		return

	html = frappe.db.get_value("Print Format", FORMAT, "html") or ""
	if MARKER in html or CSS_ANCHOR not in html:
		return

	frappe.db.set_value("Print Format", FORMAT, "html", html.replace(CSS_ANCHOR, CSS_NEW, 1))

import re

import frappe


FORMAT = "Official Receipt with Descriptions"
CSS_ANCHOR = "\t.items-subtable {"
CSS_ADD = (
	"\t.amount-table td { vertical-align: top; }\n"
	"\t.amount-table th.r-text-right { text-align: right; }\n"
	"\t.amount-table th.r-text-center { text-align: center; }\n"
	"\t.items-subtable { table-layout: fixed; }\n"
	"\t.line-descriptions { white-space: pre-wrap; overflow-wrap: break-word; margin-top: 4px; font-size: 10.5px; color: #444; }\n"
)
FIELD_DIV = re.compile(r'<div class="field" style="[^"]*?">\{\{ doc\.custom_line_descriptions or "" \}\}</div>', re.S)


def execute():
	"""Keep the receipt's line items aligned and stop custom line descriptions floating (absolute-positioned) over the table."""
	if not frappe.db.exists("Print Format", FORMAT):
		return

	html = frappe.db.get_value("Print Format", FORMAT, "html") or ""
	if ".line-descriptions" in html:
		return

	html = FIELD_DIV.sub('<div class="line-descriptions">{{ doc.custom_line_descriptions or "" }}</div>', html)
	html = html.replace(CSS_ANCHOR, CSS_ADD + CSS_ANCHOR, 1)
	frappe.db.set_value("Print Format", FORMAT, "html", html)

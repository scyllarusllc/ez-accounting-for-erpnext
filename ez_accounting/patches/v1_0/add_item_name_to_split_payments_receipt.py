import frappe

TARGET_FORMAT = "Official Receipt with Split Payments"
OLD_STYLE_ANCHOR = "\t.meta-value { font-weight: bold; width: 34%; }\n"
NEW_STYLE_ADDITION = "\t.item-description { font-size: 10px; color: #777; margin-top: 3px; }\n"
OLD_CELL_SNIPPET = "{{ item.description or item.item_name }}"
NEW_CELL_SNIPPET = (
	"{{ item.item_name }}"
	'{%- if item.description and item.description != item.item_name %}'
	'<div class="item-description">{{ item.description }}</div>'
	"{%- endif %}"
)


def execute():
	"""The items table on this receipt showed ONLY item.description, falling
	back to item.item_name when a document had no description -- so any item
	that did have a description lost its name entirely. Show the item name
	on its own line first (matching the "Sales Invoice with Descriptions"
	convention: name above, description below in smaller muted text), rather
	than one hiding the other.
	"""
	if not frappe.db.exists("Print Format", TARGET_FORMAT):
		return

	pf = frappe.get_doc("Print Format", TARGET_FORMAT)
	html = pf.html or ""
	if NEW_CELL_SNIPPET in html:
		return

	if OLD_CELL_SNIPPET not in html:
		frappe.throw(f"Expected item cell snippet was not found in Print Format {TARGET_FORMAT}")
	html = html.replace(OLD_CELL_SNIPPET, NEW_CELL_SNIPPET, 1)

	if OLD_STYLE_ANCHOR in html:
		html = html.replace(OLD_STYLE_ANCHOR, OLD_STYLE_ANCHOR + NEW_STYLE_ADDITION, 1)

	pf.html = html
	pf.save(ignore_permissions=True)
	frappe.db.commit()

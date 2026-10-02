import frappe


SOURCE_FORMAT = "Official Receipt with line descriptions"
TARGET_FORMAT = "Official Receipt with line descriptions_no_inv"
REFERENCE_HEADING = '<div>{{ row.reference_doctype }} — {{ row.reference_name }}</div>'


def execute():
	"""Create the itemized receipt variant without its invoice reference heading."""
	if not frappe.db.exists("DocType", "Payment Entry") or not frappe.db.exists("Print Format", SOURCE_FORMAT):
		return

	source = frappe.get_doc("Print Format", SOURCE_FORMAT)
	if REFERENCE_HEADING not in (source.html or ""):
		frappe.throw(f"Expected reference heading was not found in Print Format {SOURCE_FORMAT}")

	html = source.html.replace(REFERENCE_HEADING, "", 1)
	if frappe.db.exists("Print Format", TARGET_FORMAT):
		target = frappe.get_doc("Print Format", TARGET_FORMAT)
		target.html = html
		target.css = source.css
		target.disabled = 0
		target.save(ignore_permissions=True)
		return

	target = frappe.copy_doc(source)
	target.name = TARGET_FORMAT
	target.html = html
	target.disabled = 0
	target.insert(ignore_permissions=True)

"""Keep the compact bank-deposit print format aligned with the app template."""

import frappe


def execute():

	if not frappe.db.exists("Print Format", "Bank Deposit Compact"):
		return

	path = frappe.get_app_path("forms", "templates", "includes", "bank_deposit_compact.html")
	with open(path, encoding="utf-8") as template_file:
		html = template_file.read()

	print_format = frappe.get_doc("Print Format", "Bank Deposit Compact")
	if print_format.doc_type != "Forms Bank Deposit Sheet" or print_format.html == html:
		return

	print_format.html = html
	print_format.save(ignore_permissions=True)

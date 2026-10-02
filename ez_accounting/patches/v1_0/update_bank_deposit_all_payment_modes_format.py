"""Keep the all-payment-modes bank deposit format aligned with the app
template (mirrors update_bank_deposit_compact_format.py's own pattern) --
picks up the bigger/bolder company name style without needing a full
re-create of the print format record.
"""

import frappe

FORMAT_NAME = "Bank Deposit Compact_with_all_payment_modes"


def execute():
	if not frappe.db.exists("Print Format", FORMAT_NAME):
		return

	path = frappe.get_app_path(
		"forms", "templates", "includes", "bank_deposit_compact_all_payment_modes.html"
	)
	with open(path, encoding="utf-8") as template_file:
		html = template_file.read()

	print_format = frappe.get_doc("Print Format", FORMAT_NAME)
	if print_format.doc_type != "Forms Bank Deposit Sheet" or print_format.html == html:
		return

	print_format.html = html
	print_format.save(ignore_permissions=True)
	frappe.db.commit()

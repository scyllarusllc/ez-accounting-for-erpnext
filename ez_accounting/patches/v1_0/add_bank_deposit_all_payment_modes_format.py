"""Install the one-copy bank deposit format with an all-payments second page."""

import frappe


FORMAT_NAME = "Bank Deposit Compact_with_all_payment_modes"


def execute():
	path = frappe.get_app_path(
		"forms", "templates", "includes", "bank_deposit_compact_all_payment_modes.html"
	)
	with open(path, encoding="utf-8") as template_file:
		html = template_file.read()

	if frappe.db.exists("Print Format", FORMAT_NAME):
		print_format = frappe.get_doc("Print Format", FORMAT_NAME)
	else:
		print_format = frappe.new_doc("Print Format")
		print_format.name = FORMAT_NAME

	print_format.update(
		{
			"doc_type": "Forms Bank Deposit Sheet",
			"module": "Ez Accounting",
			"custom_format": 1,
			"print_format_type": "Jinja",
			"html": html,
			"disabled": 0,
		}
	)
	print_format.save(ignore_permissions=True)

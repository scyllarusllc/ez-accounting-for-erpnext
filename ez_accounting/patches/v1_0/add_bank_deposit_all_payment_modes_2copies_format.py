"""Install the two-copy (Bank copy / Accountant copy) variant of the
all-payment-modes bank deposit format -- each copy is a full, unshrunk
rendering of the report (deposit page + other-payments page), stacked with a
page break between the two copies, since this report is already dense
enough at full size that squeezing two half-height copies onto one sheet
(the way the small Payment Entry receipt formats do) would be unreadable.
"""

import frappe

FORMAT_NAME = "Bank Deposit Compact_with_all_payment_modes_2copies"


def execute():
	if not frappe.db.exists("DocType", "Forms Bank Deposit Sheet"):
		return

	path = frappe.get_app_path(
		"forms", "templates", "includes", "bank_deposit_compact_all_payment_modes_2copies.html"
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
	frappe.db.commit()

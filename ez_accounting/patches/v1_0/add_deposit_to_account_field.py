import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field


def execute():
	"""Per-item "Deposit to" bank account, managed from /forms/user/items and
	used as the receipt filter on /forms/user/deposit. Stored per Item +
	Company, so it lives on Item Default alongside income_account /
	expense_account (which /forms/user/items already edits the same way).

	create_custom_field() is idempotent — a second run (or a fresh install
	that already has it) is a no-op.
	"""
	create_custom_field(
		"Item Default",
		{
			"fieldname": "custom_deposit_to_account",
			"label": "Deposit To Account",
			"fieldtype": "Link",
			"options": "Account",
			"insert_after": "income_account",
		},
	)
	frappe.db.commit()

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field


def execute():
	"""Per-receipt "deposited" flag: a Link on Payment Entry to the Forms Bank
	Deposit Sheet that claimed it. Set on the sheet's submit, cleared on its
	cancel (see FormsBankDepositSheet). "Undeposited" is then just
	`custom_deposit_sheet IS NULL`, replacing the old date-range reconciliation.
	Same read-only tracking-field shape as ERPNext's own `clearance_date`.

	create_custom_field() is idempotent — a re-run (or a fresh install that
	already has it) is a no-op.
	"""
	create_custom_field(
		"Payment Entry",
		{
			"fieldname": "custom_deposit_sheet",
			"label": "Deposit Sheet",
			"fieldtype": "Link",
			"options": "Forms Bank Deposit Sheet",
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 1,
			"insert_after": "reference_no",
		},
	)
	frappe.db.commit()

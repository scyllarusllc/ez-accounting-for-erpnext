import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field


def execute():
	"""Per-company, permanent sequence number for every Forms Bank Deposit
	Sheet that's ever been submitted -- <abbr>-DP-00001, <abbr>-DP-00002, ...
	Same shape as Payment Entry's own custom_sequence_number (see
	ez_accounting.api.assign_deposit_sequence_number, hooked on this doctype's
	on_submit, and [[forms-payments-sequence]]) except for the "DP" segment,
	which keeps a deposit sheet's numbering in its own series, distinct from
	(and never colliding with) the plain Payment Entry sequence.

	create_custom_field() is idempotent -- a re-run (or a fresh install that
	already has it) is a no-op.
	"""
	create_custom_field(
		"Forms Bank Deposit Sheet",
		{
			"fieldname": "custom_sequence_number",
			"label": "Sequence No",
			"fieldtype": "Data",
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 1,
			"insert_after": "deposit_date",
		},
	)
	frappe.db.commit()

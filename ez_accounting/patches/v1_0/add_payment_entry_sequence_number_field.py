import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field


def execute():
	"""Per-company, permanent receipt/voucher number for every Payment Entry
	that's ever been submitted -- <abbr>-00001, <abbr>-00002, ... Assigned
	once (see ez_accounting.api.assign_payment_sequence_number, hooked on
	Payment Entry's on_submit) and never reassigned, even if the entry is
	later cancelled -- same "the number stays even when voided" rule
	Check Register already follows for check numbers.

	create_custom_field() is idempotent -- a re-run (or a fresh install that
	already has it) is a no-op.
	"""
	create_custom_field(
		"Payment Entry",
		{
			"fieldname": "custom_sequence_number",
			"label": "Sequence No",
			"fieldtype": "Data",
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 1,
			"insert_after": "reference_no",
		},
	)
	frappe.db.commit()

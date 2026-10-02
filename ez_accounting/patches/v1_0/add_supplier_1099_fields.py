import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field


def execute():
	"""1099 classification on Supplier, next to the shipped `irs_1099` checkbox
	(a Check field: "Is IRS 1099 reporting required for supplier?"). Feeds the
	/forms/user/vendor_1099 report's "1099 Type" / "1099 Box" / "Box Desc"
	columns. create_custom_field() is idempotent.
	"""
	create_custom_field(
		"Supplier",
		{
			"fieldname": "custom_1099_type",
			"label": "1099 Type",
			"fieldtype": "Select",
			"options": "\nNEC\nMISC",
			"insert_after": "irs_1099",
		},
	)
	create_custom_field(
		"Supplier",
		{
			"fieldname": "custom_1099_box",
			"label": "1099 Box",
			"fieldtype": "Data",
			"insert_after": "custom_1099_type",
			"description": "1099 box number, e.g. 1 for NEC (Nonemployee compensation).",
		},
	)
	frappe.db.commit()

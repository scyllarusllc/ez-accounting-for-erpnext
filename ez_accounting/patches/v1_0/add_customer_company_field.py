import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field


def execute():
	create_custom_field(
		"Customer",
		{
			"fieldname": "custom_company",
			"label": "Company",
			"fieldtype": "Link",
			"options": "Company",
			"insert_after": "customer_group",
		},
	)

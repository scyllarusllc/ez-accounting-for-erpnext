import frappe


def execute():
	"""Uppercase existing Company-type customer display names.

	Customer docnames are deliberately preserved: historical imports can have
	aliases whose docname differs from ``customer_name``, and changing those
	identifiers would add needless churn to linked accounting documents.
	"""
	frappe.db.sql(
		"""
		UPDATE `tabCustomer`
		SET customer_name = UPPER(customer_name)
		WHERE customer_type = 'Company'
		  AND BINARY customer_name <> BINARY UPPER(customer_name)
		"""
	)

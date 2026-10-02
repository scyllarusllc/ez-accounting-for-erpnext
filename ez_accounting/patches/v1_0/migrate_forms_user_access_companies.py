import json

import frappe


def execute():
	"""Forms User Access grew from a single `company` Link to a `companies`
	child table (any subset of companies, not just one/all). Carry each
	existing record's old single company into the new table, and seed
	`synced_companies` to match so the controller's own sync sees no diff and
	doesn't churn the User Permission it already created.

	Idempotent: skips any record that already has `companies` rows, and is a
	no-op once the legacy `company` column is gone.
	"""
	if "company" not in frappe.db.get_table_columns("Forms User Access"):
		return

	rows = frappe.db.sql(
		"""
		SELECT name, company
		FROM `tabForms User Access`
		WHERE company IS NOT NULL AND company != ''
		""",
		as_dict=True,
	)
	for row in rows:
		doc = frappe.get_doc("Forms User Access", row.name)
		if doc.companies:
			continue
		if not frappe.db.exists("Company", row.company):
			continue
		doc.append("companies", {"company": row.company})
		doc.synced_companies = json.dumps([row.company])
		doc.save(ignore_permissions=True)

	frappe.db.commit()

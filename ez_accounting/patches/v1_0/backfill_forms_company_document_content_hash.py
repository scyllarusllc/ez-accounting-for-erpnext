import frappe


def execute():
	"""Fill the new content_hash (MD5) field on every pre-existing Forms
	Company Document from its already-attached File record -- Frappe computes
	File.content_hash automatically on save, so nothing needs re-hashing here,
	only copying over for records created before this field existed. Without
	this, only documents uploaded after this patch would be usable for the
	admin Finder's "already filed" match against Documents from Email.
	"""
	if not frappe.db.has_column("Forms Company Document", "content_hash"):
		return

	rows = frappe.get_all(
		"Forms Company Document",
		filters=[["content_hash", "is", "not set"], ["file", "is", "set"]],
		fields=["name"],
	)

	updated = 0
	for row in rows:
		file_hash = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Forms Company Document", "attached_to_name": row.name},
			pluck="content_hash",
			order_by="creation desc",
			limit_page_length=1,
		)
		if file_hash and file_hash[0]:
			frappe.db.set_value("Forms Company Document", row.name, "content_hash", file_hash[0], update_modified=False)
			updated += 1

	frappe.db.commit()
	print(f"backfill_forms_company_document_content_hash: updated {updated} of {len(rows)} documents")

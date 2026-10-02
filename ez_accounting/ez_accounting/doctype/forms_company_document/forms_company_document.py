# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class FormsCompanyDocument(Document):
	def before_insert(self):
		# The uploader, not whoever might edit the record's metadata later —
		# set once, same "recorded at creation, never reassigned" convention
		# custom_sequence_number follows elsewhere in this app.
		if not self.uploaded_by:
			self.uploaded_by = frappe.session.user

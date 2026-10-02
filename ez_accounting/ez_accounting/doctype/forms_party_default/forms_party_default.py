# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class FormsPartyDefault(Document):
	def validate(self):
		# One row per (company, party_type, party) — ez_accounting.api.upsert_party_default
		# always fetches-then-updates the existing row, so this only guards
		# against a stray duplicate created some other way (e.g. by hand in Desk).
		duplicate = frappe.db.exists(
			"Forms Party Default",
			{
				"company": self.company,
				"party_type": self.party_type,
				"party": self.party,
				"name": ["!=", self.name],
			},
		)
		if duplicate:
			frappe.throw(
				_("A default for {0} {1} already exists for {2}: {3}").format(
					self.party_type, self.party, self.company, duplicate
				)
			)

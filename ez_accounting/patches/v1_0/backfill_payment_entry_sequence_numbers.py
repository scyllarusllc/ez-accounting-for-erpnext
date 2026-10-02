import frappe
from frappe.model.naming import getseries


def execute():
	"""Assign every already-submitted (or since-cancelled) Payment Entry the
	same permanent <abbr>-00001 sequence number new ones get automatically
	going forward (see ez_accounting.api.assign_payment_sequence_number) -- without
	this, only Payment Entries submitted after this patch would ever have
	one.

	Walks each Company's Payment Entries in the order they were actually
	entered into this system (`creation` ascending) -- not `posting_date`,
	which is the real (often backdated) transaction date and can differ
	wildly from when it was actually recorded here (this site's own history
	is years of historical transactions entered progressively over a few
	weeks in 2026). #1 is that company's *first-recorded* payment, matching
	what a real receipt book's numbering reflects (the order things were
	actually written down), not what each one claims happened. getseries()
	itself only hands out the next atomic number in call order, it has no
	idea about dates either way.
	"""
	if not frappe.db.has_column("Payment Entry", "custom_sequence_number"):
		return

	numbered = 0
	for company in frappe.get_all("Company", fields=["name", "abbr"]):
		if not company.abbr:
			continue

		pending = frappe.get_all(
			"Payment Entry",
			filters={
				"company": company.name,
				"docstatus": ["in", [1, 2]],
				"custom_sequence_number": ["in", ["", None]],
			},
			fields=["name"],
			order_by="creation asc",
		)
		for pe in pending:
			number = getseries(f"FORMS-PAYMENT-{company.abbr}-", 5)
			frappe.db.set_value(
				"Payment Entry", pe.name, "custom_sequence_number", f"{company.abbr}-{number}", update_modified=False
			)
			numbered += 1

	frappe.db.commit()
	print(f"backfill_payment_entry_sequence_numbers: numbered {numbered} Payment Entries")

import frappe
from frappe.model.naming import getseries


def execute():
	"""Assign every already-submitted (or since-cancelled) Forms Bank Deposit
	Sheet the same permanent <abbr>-DP-00001 sequence number new ones get
	automatically going forward (see ez_accounting.api.assign_deposit_sequence_number)
	-- without this, only sheets submitted after this patch would ever have
	one.

	Walks each Company's sheets in `creation` order (when the sheet was
	actually submitted here), same rule -- and same reasoning -- as
	backfill_payment_entry_sequence_numbers.py: this site's own history was
	entered progressively, so `creation` order is what's meaningful, not
	`deposit_date` (which can be backdated same as posting_date can for a
	Payment Entry).
	"""
	if not frappe.db.has_column("Forms Bank Deposit Sheet", "custom_sequence_number"):
		return

	numbered = 0
	for company in frappe.get_all("Company", fields=["name", "abbr"]):
		if not company.abbr:
			continue

		pending = frappe.get_all(
			"Forms Bank Deposit Sheet",
			filters={
				"company": company.name,
				"docstatus": ["in", [1, 2]],
				"custom_sequence_number": ["in", ["", None]],
			},
			fields=["name"],
			order_by="creation asc",
		)
		series_key = f"FORMS-DEPOSIT-{company.abbr}-"
		for sheet in pending:
			number = getseries(series_key, 5)
			frappe.db.set_value(
				"Forms Bank Deposit Sheet",
				sheet.name,
				"custom_sequence_number",
				f"{company.abbr}-DP-{number}",
				update_modified=False,
			)
			numbered += 1

	frappe.db.commit()
	print(f"backfill_deposit_sheet_sequence_numbers: numbered {numbered} Forms Bank Deposit Sheets")

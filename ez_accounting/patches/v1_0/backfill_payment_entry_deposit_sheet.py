import frappe


def execute():
	"""Stamp custom_deposit_sheet onto the Payment Entries the already-submitted
	deposit sheets covered, so "Not Yet Deposited" / Expected Cash start from a
	correct baseline instead of re-listing every historical receipt.

	Walks submitted sheets oldest-first and, for each, claims the still-
	unclaimed Cash/Check receipts in its receipt period (same set
	FormsBankDepositSheet._covered_payment_entry_names() computes at submit
	time). Oldest-first so an earlier sheet claims a receipt before a later
	overlapping one — matching the runtime "first sheet to cover wins" rule.
	"""
	if not frappe.db.has_column("Payment Entry", "custom_deposit_sheet"):
		return

	from ez_accounting.ez_accounting.doctype.forms_bank_deposit_sheet.forms_bank_deposit_sheet import _get_payment_entries

	sheets = frappe.get_all(
		"Forms Bank Deposit Sheet",
		filters={"docstatus": 1},
		fields=[
			"name",
			"company",
			"from_receipt_date",
			"to_receipt_date",
			"cash_account",
			"checks_account",
			"deposit_to_account",
		],
		order_by="creation asc",
	)

	stamped = 0
	for s in sheets:
		names = set()
		for mode_keyword, account in (("Cash", s.cash_account), ("Check", s.checks_account)):
			for entry in _get_payment_entries(
				s.company,
				s.from_receipt_date,
				s.to_receipt_date,
				mode_keyword,
				account,
				s.deposit_to_account,
				undeposited_only=True,
			):
				names.add(entry["name"])
		for name in names:
			frappe.db.set_value("Payment Entry", name, "custom_deposit_sheet", s.name, update_modified=False)
			stamped += 1

	frappe.db.commit()
	print(f"backfill_payment_entry_deposit_sheet: stamped {stamped} Payment Entries across {len(sheets)} sheets")

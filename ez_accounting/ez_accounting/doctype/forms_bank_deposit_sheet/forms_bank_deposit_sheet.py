# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


def _get_payment_entries(
	company: str,
	from_receipt_date,
	to_receipt_date,
	mode_keyword: str,
	account: str | None,
	deposit_to_account: str | None = None,
	undeposited_only: bool = False,
) -> list:
	"""The actual Payment Entries a cash/checks expected-amount figure is
	summed from — classified by Mode of Payment name (matched against
	mode_keyword, e.g. "Cash" or "Check") rather than by account alone: some
	companies here stage *both* cash and checks in the very same
	"Undeposited Funds" account, so filtering by account alone can't tell
	which portion of it is cash vs checks; only the Mode of Payment can. The
	account, when given, narrows further on top of that (a company may have
	more than one Cash-type account) rather than substituting for the mode
	check. Shared by _get_expected_amount() (which just sums paid_amount over
	this) and the deposit page's own preview list, so the number shown and
	the entries shown to explain it can never drift apart.

	deposit_to_account, when given, narrows to Payment Entries allocated
	against a Sales Invoice that has at least one line item bound to that
	account via Item Default → custom_deposit_to_account (the per-item
	"Deposit to" set on /forms/user/items). This is what lets a company
	reconcile one bank account's revenue stream at a time. Item.name equals
	its item_code on this site (Stock Settings "Item Naming By" = Item Code),
	so Item Default.parent joins directly to Sales Invoice Item.item_code.

	undeposited_only, when True, excludes Payment Entries already claimed by a
	submitted deposit sheet (custom_deposit_sheet set) — the reconciliation
	the Expected Cash/Checks figures and the "Not Yet Deposited" panel are
	built on.
	"""
	values = {
		"company": company,
		"from_date": from_receipt_date,
		"to_date": to_receipt_date,
		"mode_keyword": f"%{mode_keyword}%",
	}
	party_condition = ""
	if account:
		values["account"] = account
		party_condition = "AND paid_to = %(account)s"

	undeposited_condition = ""
	if undeposited_only:
		undeposited_condition = "AND (custom_deposit_sheet IS NULL OR custom_deposit_sheet = '')"

	deposit_to_condition = ""
	if deposit_to_account:
		values["deposit_to_account"] = deposit_to_account
		deposit_to_condition = """
			AND name IN (
				SELECT per.parent
				FROM `tabPayment Entry Reference` per
				JOIN `tabSales Invoice Item` sii ON sii.parent = per.reference_name
				JOIN `tabItem Default` idf
					ON idf.parent = sii.item_code AND idf.company = %(company)s
				WHERE per.reference_doctype = 'Sales Invoice' AND per.docstatus = 1
					AND idf.custom_deposit_to_account = %(deposit_to_account)s
			)
		"""

	return frappe.db.sql(
		f"""
		SELECT name, company, posting_date, party_type, party, party_name, paid_amount,
			mode_of_payment, paid_to, reference_no, remarks, custom_line_descriptions
		FROM `tabPayment Entry`
		WHERE docstatus = 1 AND payment_type = 'Receive' AND company = %(company)s
			AND posting_date BETWEEN %(from_date)s AND %(to_date)s
			AND mode_of_payment LIKE %(mode_keyword)s
			{party_condition}
			{deposit_to_condition}
			{undeposited_condition}
		ORDER BY posting_date ASC, creation ASC
		""",
		values,
		as_dict=True,
	)


def _get_expected_amount(
	company: str,
	from_receipt_date,
	to_receipt_date,
	mode_keyword: str,
	account: str | None,
	deposit_to_account: str | None = None,
) -> float:
	"""What a deposit sheet for this receipt period should contain: the
	paid_amount of every matching Payment Entry (see _get_payment_entries())
	that hasn't already been claimed by a submitted deposit sheet. A plain
	sum now — the old "minus what prior sheets deposited for this exact date
	range" subtraction is replaced by the per-receipt custom_deposit_sheet
	flag (stamped in FormsBankDepositSheet.on_submit, cleared on_cancel).
	"""
	entries = _get_payment_entries(
		company, from_receipt_date, to_receipt_date, mode_keyword, account, deposit_to_account, undeposited_only=True
	)
	return sum(flt(e.paid_amount) for e in entries)


def get_expected_cash(
	company: str, from_receipt_date, to_receipt_date, cash_account: str | None = None, deposit_to_account: str | None = None
) -> float:
	return _get_expected_amount(
		company, from_receipt_date, to_receipt_date, "Cash", cash_account, deposit_to_account
	)


def get_expected_checks(
	company: str, from_receipt_date, to_receipt_date, checks_account: str | None = None, deposit_to_account: str | None = None
) -> float:
	return _get_expected_amount(
		company, from_receipt_date, to_receipt_date, "Check", checks_account, deposit_to_account
	)


def get_cash_payment_entries(
	company: str, from_receipt_date, to_receipt_date, cash_account: str | None = None, deposit_to_account: str | None = None
) -> list:
	return _get_payment_entries(
		company, from_receipt_date, to_receipt_date, "Cash", cash_account, deposit_to_account, undeposited_only=True
	)


def get_checks_payment_entries(
	company: str, from_receipt_date, to_receipt_date, checks_account: str | None = None, deposit_to_account: str | None = None
) -> list:
	return _get_payment_entries(
		company, from_receipt_date, to_receipt_date, "Check", checks_account, deposit_to_account, undeposited_only=True
	)


def get_undeposited_payment_entries(company: str, deposit_to_account: str | None = None) -> list:
	"""Every Cash- or Check-mode Receive Payment Entry for this company not yet
	claimed by a submitted deposit sheet (custom_deposit_sheet still NULL) —
	the receipts finance still has to deposit. When deposit_to_account is
	given, narrowed to those whose Sales Invoice has a line item bound to that
	account (same Item Default → custom_deposit_to_account join as
	_get_payment_entries()).
	"""
	values = {"company": company}

	deposit_to_condition = ""
	if deposit_to_account:
		values["deposit_to_account"] = deposit_to_account
		deposit_to_condition = """
			AND pe.name IN (
				SELECT per.parent
				FROM `tabPayment Entry Reference` per
				JOIN `tabSales Invoice Item` sii ON sii.parent = per.reference_name
				JOIN `tabItem Default` idf
					ON idf.parent = sii.item_code AND idf.company = %(company)s
				WHERE per.reference_doctype = 'Sales Invoice' AND per.docstatus = 1
					AND idf.custom_deposit_to_account = %(deposit_to_account)s
			)
		"""

	return frappe.db.sql(
		f"""
		SELECT pe.name, pe.company, pe.posting_date, pe.party_type, pe.party, pe.party_name,
			pe.paid_amount, pe.mode_of_payment, pe.paid_to, pe.reference_no,
			(
				SELECT GROUP_CONCAT(DISTINCT idf.custom_deposit_to_account
					ORDER BY idf.custom_deposit_to_account SEPARATOR ', ')
				FROM `tabPayment Entry Reference` per
				JOIN `tabSales Invoice Item` sii ON sii.parent = per.reference_name
				JOIN `tabItem Default` idf ON idf.parent = sii.item_code AND idf.company = pe.company
				WHERE per.parent = pe.name AND per.reference_doctype = 'Sales Invoice'
					AND per.docstatus = 1 AND IFNULL(idf.custom_deposit_to_account, '') != ''
			) AS deposit_to_account,
			CASE WHEN pe.mode_of_payment LIKE '%%Check%%' THEN 'Check' ELSE 'Cash' END AS bucket
		FROM `tabPayment Entry` pe
		WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive' AND pe.company = %(company)s
			AND (pe.mode_of_payment LIKE '%%Cash%%' OR pe.mode_of_payment LIKE '%%Check%%')
			AND (pe.custom_deposit_sheet IS NULL OR pe.custom_deposit_sheet = '')
			{deposit_to_condition}
		ORDER BY pe.posting_date ASC, pe.creation ASC
		""",
		values,
		as_dict=True,
	)


class FormsBankDepositSheet(Document):
	def validate(self):
		# Recomputed server-side rather than trusted from the client — the
		# denomination count is the actual source of truth for what was
		# physically counted; System Expected Cash and Variance both follow
		# from it (and from live data), not from whatever a browser sent.
		self.total_physical_cash = (
			flt(self.usd100) * 100
			+ flt(self.usd50) * 50
			+ flt(self.usd20) * 20
			+ flt(self.usd10) * 10
			+ flt(self.usd5) * 5
			+ flt(self.usd1) * 1
			+ flt(self.coins)
		)

		self.system_expected_cash = get_expected_cash(
			self.company, self.from_receipt_date, self.to_receipt_date, self.cash_account, self.deposit_to_account
		)

		self.variance = flt(self.total_physical_cash) - flt(self.system_expected_cash)

		self.system_expected_checks = get_expected_checks(
			self.company, self.from_receipt_date, self.to_receipt_date, self.checks_account, self.deposit_to_account
		)

		self.check_variance = flt(self.check_amount) - flt(self.system_expected_checks)

		self.total_deposit = flt(self.total_physical_cash) + flt(self.check_amount)
		self.total_expected = flt(self.system_expected_cash) + flt(self.system_expected_checks)
		self.total_variance = flt(self.variance) + flt(self.check_variance)

	def on_submit(self):
		self._mark_deposited_payment_entries()
		self._post_deposit_journal_entry()

	def on_cancel(self):
		self.ignore_linked_doctypes = ("GL Entry", "Journal Entry", "Payment Entry")
		self._release_deposited_payment_entries()
		if self.deposit_journal_entry:
			je = frappe.get_doc("Journal Entry", self.deposit_journal_entry)
			if je.docstatus == 1:
				# This Journal Entry is an implementation detail of the deposit
				# sheet.  The user has already passed this doctype's Cancel
				# permission check; do not require broad Journal Entry access too.
				je.flags.ignore_permissions = True
				je.cancel()

	def _covered_payment_entry_names(self) -> set:
		"""The still-unclaimed Cash/Check receipts this sheet's receipt period
		covers — exactly the set validate() summed into system_expected_cash /
		system_expected_checks (cash mode + cash_account, check mode +
		checks_account, both scoped to deposit_to_account)."""
		names = set()
		for mode_keyword, account in (
			("Cash", self.cash_account),
			("Check", self.checks_account),
		):
			for entry in _get_payment_entries(
				self.company,
				self.from_receipt_date,
				self.to_receipt_date,
				mode_keyword,
				account,
				self.deposit_to_account,
				undeposited_only=True,
			):
				names.add(entry["name"])
		return names

	def get_receipt_details(self) -> list:
		"""The individual Cash/Check receipts this deposit sheet represents — for
		the "Bank Deposit Compact" print format's "Daily Receipts Details" table
		(and its DEPOSIT TOTAL). Scoped exactly like validate()'s
		system_expected_cash / system_expected_checks: this company, the receipt
		date range, the cash/checks account, the Mode of Payment, AND the sheet's
		"Deposit to" account (the per-item Item Default -> custom_deposit_to_account
		binding) — so the printed list and the printed expected figures agree,
		and receipts for other companies / other bank accounts never leak in.

		A submitted sheet reads back its own stamped receipts
		(custom_deposit_sheet = name), which is the exact set it claimed; a draft
		(or a cancelled sheet whose stamps were released) re-derives what it would
		claim from the same helper _covered_payment_entry_names() uses.
		"""
		if self.docstatus == 1:
			rows = frappe.get_all(
				"Payment Entry",
				filters={"custom_deposit_sheet": self.name},
				fields=[
					"name", "party_name", "posting_date", "mode_of_payment", "paid_amount",
					"reference_no", "remarks", "custom_line_descriptions",
				],
				order_by="posting_date asc, creation asc",
			)
			for row in rows:
				row["remarks"] = row.custom_line_descriptions or row.remarks
			return rows

		seen = set()
		rows = []
		for mode_keyword, account in (
			("Cash", self.cash_account),
			("Check", self.checks_account),
		):
			for entry in _get_payment_entries(
				self.company,
				self.from_receipt_date,
				self.to_receipt_date,
				mode_keyword,
				account,
				self.deposit_to_account,
				undeposited_only=True,
			):
				if entry["name"] in seen:
					continue
				seen.add(entry["name"])
				rows.append(entry)
		rows.sort(key=lambda r: (r["posting_date"] or "", r["name"]))
		for row in rows:
			row["remarks"] = row.custom_line_descriptions or row.remarks
		return rows

	def get_other_period_payments(self) -> list:
		"""Incoming payments in the period that are not undeposited cash/checks.

		It matches Payment Entry.paid_to to this sheet's Deposit To account, then
		supplies the remainder after the Cash Undeposited and Check Undeposited
		modes represented by the first table are excluded.
		"""
		filters = {
			"company": self.company,
			"payment_type": "Receive",
			"docstatus": 1,
			"posting_date": ["between", [self.from_receipt_date, self.to_receipt_date]],
		}
		if self.deposit_to_account:
			filters["paid_to"] = self.deposit_to_account

		rows = frappe.get_all(
			"Payment Entry",
			filters=filters,
			fields=[
				"name", "posting_date", "party_name", "mode_of_payment", "paid_to", "paid_amount",
				"reference_no", "remarks", "custom_line_descriptions",
			],
			order_by="posting_date asc, creation asc",
		)
		# Receipts already listed in the deposit table must not be repeated here,
		# or the same money shows up twice (e.g. a check paid straight into the bank).
		deposited = {r.name for r in self.get_receipt_details()}
		other_rows = []
		for row in rows:
			if row.name in deposited:
				continue
			mode = (row.mode_of_payment or "").lower()
			if "undeposited" in mode and ("cash" in mode or "check" in mode):
				continue
			if "credit" in mode and "card" in mode:
				row["payment_bucket"] = "credit_card"
			elif "wire" in mode:
				row["payment_bucket"] = "wire_transfer"
			else:
				row["payment_bucket"] = "other_payment"
			row["description"] = row.custom_line_descriptions or row.remarks or ""
			other_rows.append(row)
		return other_rows

	def _mark_deposited_payment_entries(self):
		"""Stamp this sheet's name onto every receipt it covers — the
		per-Payment-Entry replacement for the old date-range "already
		deposited" math. Direct field write (update_modified=False), same as
		ERPNext's own Bank Clearance sets clearance_date."""
		for name in self._covered_payment_entry_names():
			frappe.db.set_value("Payment Entry", name, "custom_deposit_sheet", self.name, update_modified=False)

	def _release_deposited_payment_entries(self):
		for name in frappe.get_all(
			"Payment Entry", filters={"custom_deposit_sheet": self.name}, pluck="name"
		):
			frappe.db.set_value("Payment Entry", name, "custom_deposit_sheet", None, update_modified=False)

	def _post_deposit_journal_entry(self):
		"""The actual money movement this sheet represents: what was physically
		counted (Total Physical Cash / Check Amount — not System Expected, which
		is only a reconciliation target) moving out of the Cash/Checks Account
		(typically an Undeposited Funds staging account, but could be a literal
		till) and into the Bank Account. A leg whose account *is* the Bank
		Account is skipped — that money's already there per the underlying
		Payment Entry, nothing to transfer — and identical Cash/Checks accounts
		are merged into a single credit line rather than two rows against the
		same account. If nothing ends up needing a transfer, no Journal Entry
		is created at all (deposit_journal_entry stays empty).
		"""
		transfer_total = 0.0
		credit_by_account = {}
		for amount, account in (
			(self.total_physical_cash, self.cash_account),
			(self.check_amount, self.checks_account),
		):
			amount = flt(amount)
			if amount <= 0 or account == self.bank_account:
				continue
			transfer_total += amount
			credit_by_account[account] = credit_by_account.get(account, 0.0) + amount

		if transfer_total <= 0:
			return

		cost_center = frappe.get_cached_value("Company", self.company, "cost_center")

		def je_row(account, debit=0, credit=0):
			# No reference_type/reference_name back to this doctype: Journal
			# Entry Account's reference_type is a fixed Select whose options
			# don't include custom doctypes like this one (confirmed by
			# hitting ValidationError: Row #1: Reference Type cannot be
			# "Forms Bank Deposit Sheet" while testing) — the deposit_journal_entry
			# link field on this doc already ties the two together.
			row = {"account": account}
			if debit:
				row["debit_in_account_currency"] = debit
			if credit:
				row["credit_in_account_currency"] = credit
			if cost_center:
				row["cost_center"] = cost_center
			return row

		accounts = [je_row(self.bank_account, debit=transfer_total)]
		for account, amount in credit_by_account.items():
			accounts.append(je_row(account, credit=amount))

		bank_account_type = frappe.get_cached_value("Account", self.bank_account, "account_type")
		voucher_type = "Cash Entry" if bank_account_type == "Cash" else "Bank Entry"

		je = frappe.new_doc("Journal Entry")
		je.voucher_type = voucher_type
		je.company = self.company
		je.posting_date = self.deposit_date
		je.user_remark = _("Bank deposit {0} for receipts {1} to {2}").format(
			self.name, self.from_receipt_date, self.to_receipt_date
		)
		je.set("accounts", accounts)

		if voucher_type == "Bank Entry":
			# Journal Entry requires Reference No + Reference Date for voucher_type
			# "Bank Entry" before it can submit (validate_cheque_info() in Frappe's
			# core Journal Entry controller) — no real cheque/wire reference exists
			# here, so tie it back to the deposit sheet it came from, same
			# convention ez_accounting.www.forms.user.payroll uses for its own bank
			# disbursement entry.
			je.cheque_no = self.name
			je.cheque_date = self.deposit_date

		je.insert(ignore_permissions=True)
		# insert(ignore_permissions=True) only covers insertion.  submit()
		# performs its own permission check, so carry the narrowly scoped bypass
		# through submission of this system-generated Journal Entry as well.
		je.flags.ignore_permissions = True
		je.submit()

		self.db_set("deposit_journal_entry", je.name, update_modified=False)

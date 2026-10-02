# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


def get_clearing_balance(account: str, as_of_date) -> float:
	"""The clearing account's running GL balance (debit - credit) as of a
	date — same shape as reconciliation.py's get_book_balance(). This IS the
	un-swept amount: every sweep credits the account, so what's left is what
	still needs moving to a real expense.
	"""
	balance = frappe.db.sql(
		"""
		SELECT SUM(debit) - SUM(credit) FROM `tabGL Entry`
		WHERE account = %(account)s AND posting_date <= %(as_of_date)s AND is_cancelled = 0
		""",
		{"account": account, "as_of_date": as_of_date},
	)[0][0]
	return flt(balance)


class FormsClearingSweep(Document):
	def validate(self):
		clearing = frappe.db.get_value(
			"Account", self.clearing_account, ["company", "is_group", "account_type"], as_dict=True
		)
		if not clearing or clearing.is_group or clearing.company != self.company:
			frappe.throw(_("{0} is not a valid account for {1}.").format(self.clearing_account, self.company))
		if clearing.account_type != "Cash":
			frappe.throw(
				_("Clearing Account must be a Cash-type account. {0} is not.").format(self.clearing_account)
			)

		# The offset account is either a real Expense account or an Income
		# account used as contra-revenue (debiting it nets down gross revenue —
		# the standard non-profit way to record a tuition discount). Both are
		# valid; see ez_accounting.www.forms.user.clearing.get_offset_accounts().
		offset = frappe.db.get_value(
			"Account", self.expense_account, ["company", "is_group", "root_type"], as_dict=True
		)
		if not offset or offset.is_group or offset.company != self.company:
			frappe.throw(_("{0} is not a valid account for {1}.").format(self.expense_account, self.company))
		if offset.root_type not in ("Expense", "Income"):
			frappe.throw(
				_("The offset account must be an Expense or Income (contra-revenue) account. {0} is neither.").format(
					self.expense_account
				)
			)

		# Recomputed server-side, never trusted from the client — same reasoning
		# as Forms Bank Deposit Sheet.validate() recomputing its own totals.
		self.clearing_balance = get_clearing_balance(self.clearing_account, self.posting_date)

		self.amount = flt(self.amount)
		if self.amount <= 0:
			frappe.throw(_("Amount must be more than zero."))
		# 0.01 tolerance for currency rounding, same as this app's other
		# amount-vs-computed-total checks.
		if self.amount > flt(self.clearing_balance) + 0.01:
			frappe.throw(
				_("Amount {0} is more than the clearing balance {1} as of {2}.").format(
					self.amount, flt(self.clearing_balance), self.posting_date
				)
			)

	def on_submit(self):
		self._post_clearing_journal_entry()

	def on_cancel(self):
		self.ignore_linked_doctypes = ("GL Entry", "Journal Entry")
		if self.journal_entry:
			je = frappe.get_doc("Journal Entry", self.journal_entry)
			if je.docstatus == 1:
				je.cancel()

	def _post_clearing_journal_entry(self):
		"""Debit the Offset account (a real Expense account, or an Income
		account used as contra-revenue), Credit the Cash-type Clearing
		account, for Amount — the summary entry that clears out the fake cash
		balance the virtual "Tuition Discount" Mode of Payment left behind.
		Plain voucher_type "Journal Entry" (not the Bank/Cash-Entry types
		Forms Bank Deposit Sheet uses) — "Journal Entry" carries no cheque_no
		requirement.
		"""
		cost_center = frappe.get_cached_value("Company", self.company, "cost_center")

		def je_row(account, debit=0.0, credit=0.0):
			row = {"account": account}
			if debit:
				row["debit_in_account_currency"] = debit
			if credit:
				row["credit_in_account_currency"] = credit
			if cost_center:
				row["cost_center"] = cost_center
			return row

		je = frappe.new_doc("Journal Entry")
		je.voucher_type = "Journal Entry"
		je.company = self.company
		je.posting_date = self.posting_date
		je.user_remark = _("Clearing sweep {0}: {1} to {2}{3}").format(
			self.name,
			self.clearing_account,
			self.expense_account,
			f" ({self.remark})" if self.remark else "",
		)
		je.set(
			"accounts",
			[
				je_row(self.expense_account, debit=flt(self.amount)),
				je_row(self.clearing_account, credit=flt(self.amount)),
			],
		)
		je.insert(ignore_permissions=True)
		je.submit()

		self.db_set("journal_entry", je.name, update_modified=False)

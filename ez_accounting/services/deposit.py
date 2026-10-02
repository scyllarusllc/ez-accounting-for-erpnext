"""Deposit Management: group undeposited cash/checks into one bank deposit.

Cash and check payments often sit in an "Undeposited Funds" holding account
until someone physically takes them to the bank. This module groups the
Payment Entries that make up a single physical deposit and records the
transfer from Undeposited Funds to the real Bank Account with one Journal
Entry — mirroring how a deposit slip works in real life.

Built entirely on standard ERPNext documents (Payment Entry, Journal Entry,
Account); no changes to ERPNext core.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import frappe
from frappe.utils import flt, nowdate


@dataclass
class DepositLine:
	payment_entry: str
	amount: float


@dataclass
class BankDepositRequest:
	company: str
	bank_account: str
	undeposited_funds_account: str
	lines: list[DepositLine]
	deposit_date: str = field(default_factory=nowdate)
	reference_no: str | None = None


@dataclass
class BankDepositResult:
	journal_entry: str
	total_deposited: float
	payment_entries: list[str]


def create_bank_deposit(request: BankDepositRequest) -> BankDepositResult:
	"""Create one Journal Entry moving the sum of ``lines`` from
	Undeposited Funds into the real bank account, and mark each source
	Payment Entry as deposited.

	Raises ``frappe.ValidationError`` if ``lines`` is empty or any line's
	Payment Entry amount doesn't match what's being deposited.
	"""
	if not request.lines:
		frappe.throw("A bank deposit needs at least one payment to deposit.")

	_validate_lines(request.lines)

	total = sum(flt(line.amount) for line in request.lines)
	je = _make_journal_entry(request, total)

	for line in request.lines:
		_mark_payment_deposited(line.payment_entry, je.name)

	return BankDepositResult(
		journal_entry=je.name,
		total_deposited=total,
		payment_entries=[line.payment_entry for line in request.lines],
	)


def _validate_lines(lines: list[DepositLine]) -> None:
	for line in lines:
		pe = frappe.get_doc("Payment Entry", line.payment_entry)
		if pe.docstatus != 1:
			frappe.throw(f"{line.payment_entry} is not submitted and can't be deposited.")
		if getattr(pe, "ez_deposited_in", None):
			frappe.throw(f"{line.payment_entry} is already included in deposit {pe.ez_deposited_in}.")
		if flt(line.amount) > flt(pe.paid_amount):
			frappe.throw(
				f"Deposit amount for {line.payment_entry} ({line.amount}) exceeds its paid amount ({pe.paid_amount})."
			)


def _make_journal_entry(request: BankDepositRequest, total: float):
	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Bank Entry"
	je.company = request.company
	je.posting_date = request.deposit_date
	je.cheque_no = request.reference_no or ""
	je.cheque_date = request.deposit_date

	je.append(
		"accounts",
		{
			"account": request.bank_account,
			"debit_in_account_currency": total,
			"credit_in_account_currency": 0,
		},
	)
	je.append(
		"accounts",
		{
			"account": request.undeposited_funds_account,
			"debit_in_account_currency": 0,
			"credit_in_account_currency": total,
		},
	)

	je.insert(ignore_permissions=True)
	je.submit()
	return je


def _mark_payment_deposited(payment_entry: str, journal_entry: str) -> None:
	"""Tag the source Payment Entry with the deposit that cleared it, so it
	can't be included in a second deposit and so Reconciliation can trace
	a bank line back to the original payments.
	"""
	frappe.db.set_value("Payment Entry", payment_entry, "ez_deposited_in", journal_entry)

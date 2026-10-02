"""Unit tests for the Deposit Management service layer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import ez_accounting.services.deposit as deposit_module
from ez_accounting.services.deposit import (
	BankDepositRequest,
	DepositLine,
	create_bank_deposit,
)


def _mock_payment_entry(amount: float, deposited_in: str | None = None):
	pe = MagicMock()
	pe.docstatus = 1
	pe.paid_amount = amount
	pe.ez_deposited_in = deposited_in
	return pe


@patch("ez_accounting.services.deposit.frappe")
def test_empty_lines_raises(mock_frappe):
	mock_frappe.throw.side_effect = Exception("validation error")
	request = BankDepositRequest(
		company="Test Company",
		bank_account="Bank - TC",
		undeposited_funds_account="Undeposited Funds - TC",
		lines=[],
	)
	with pytest.raises(Exception, match="validation error"):
		create_bank_deposit(request)


@patch("ez_accounting.services.deposit.frappe")
def test_single_line_creates_balanced_journal_entry(mock_frappe):
	mock_frappe.get_doc.return_value = _mock_payment_entry(amount=250.0)
	je = MagicMock()
	je.name = "JE-0001"
	mock_frappe.new_doc.return_value = je

	request = BankDepositRequest(
		company="Test Company",
		bank_account="Bank - TC",
		undeposited_funds_account="Undeposited Funds - TC",
		lines=[DepositLine(payment_entry="PE-0001", amount=250.0)],
	)
	result = create_bank_deposit(request)

	assert result.journal_entry == "JE-0001"
	assert result.total_deposited == 250.0
	assert result.payment_entries == ["PE-0001"]

	# Debit bank, credit undeposited funds, for the same total -> balanced.
	debit_call, credit_call = je.append.call_args_list
	assert debit_call.args[1]["account"] == "Bank - TC"
	assert debit_call.args[1]["debit_in_account_currency"] == 250.0
	assert credit_call.args[1]["account"] == "Undeposited Funds - TC"
	assert credit_call.args[1]["credit_in_account_currency"] == 250.0

	mock_frappe.db.set_value.assert_called_once_with(
		"Payment Entry", "PE-0001", "ez_deposited_in", "JE-0001"
	)


@patch("ez_accounting.services.deposit.frappe")
def test_multiple_lines_sum_to_deposit_total(mock_frappe):
	mock_frappe.get_doc.side_effect = [
		_mock_payment_entry(amount=100.0),
		_mock_payment_entry(amount=75.0),
	]
	je = MagicMock()
	je.name = "JE-0002"
	mock_frappe.new_doc.return_value = je

	request = BankDepositRequest(
		company="Test Company",
		bank_account="Bank - TC",
		undeposited_funds_account="Undeposited Funds - TC",
		lines=[
			DepositLine(payment_entry="PE-0010", amount=100.0),
			DepositLine(payment_entry="PE-0011", amount=75.0),
		],
	)
	result = create_bank_deposit(request)

	assert result.total_deposited == 175.0
	assert result.payment_entries == ["PE-0010", "PE-0011"]
	assert mock_frappe.db.set_value.call_count == 2


@patch("ez_accounting.services.deposit.frappe")
def test_already_deposited_payment_is_rejected(mock_frappe):
	mock_frappe.get_doc.return_value = _mock_payment_entry(amount=100.0, deposited_in="JE-0005")
	mock_frappe.throw.side_effect = Exception("already deposited")

	request = BankDepositRequest(
		company="Test Company",
		bank_account="Bank - TC",
		undeposited_funds_account="Undeposited Funds - TC",
		lines=[DepositLine(payment_entry="PE-0099", amount=100.0)],
	)
	with pytest.raises(Exception, match="already deposited"):
		create_bank_deposit(request)


if __name__ == "__main__":
	pytest.main([__file__, "-v"])

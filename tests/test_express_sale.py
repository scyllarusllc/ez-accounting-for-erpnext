"""Unit tests for the Express Sale service layer.

These use plain mocks rather than a live Frappe site so the suite runs fast
and without a bench. See docs/roadmap.md for integration tests against a
real site.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ez_accounting.services.express_sale import (
	ExpressSaleRequest,
	SplitPayment,
	create_express_sale,
)


def _mock_invoice(outstanding: float):
	invoice = MagicMock()
	invoice.name = "SINV-0001"
	invoice.customer = "Test Customer"
	invoice.company = "Test Company"
	invoice.outstanding_amount = outstanding
	invoice.reload = MagicMock()
	return invoice


@patch("ez_accounting.services.express_sale._make_payment_entry")
@patch("ez_accounting.services.express_sale._make_sales_invoice")
def test_invoice_only_creates_no_payment(mock_make_invoice, mock_make_payment):
	mock_make_invoice.return_value = _mock_invoice(outstanding=100.0)

	request = ExpressSaleRequest(
		customer="Test Customer",
		items=[{"item_code": "ITEM-001", "qty": 1, "rate": 100.0}],
		company="Test Company",
	)
	result = create_express_sale(request)

	assert result.sales_invoice == "SINV-0001"
	assert result.payment_entries == []
	assert result.outstanding_amount == 100.0
	mock_make_payment.assert_not_called()


@patch("ez_accounting.services.express_sale._make_payment_entry")
@patch("ez_accounting.services.express_sale._make_sales_invoice")
def test_full_payment_creates_single_payment_entry(mock_make_invoice, mock_make_payment):
	mock_make_invoice.return_value = _mock_invoice(outstanding=0.0)
	mock_make_payment.return_value = MagicMock(name="PE-0001")
	mock_make_payment.return_value.name = "PE-0001"

	request = ExpressSaleRequest(
		customer="Test Customer",
		items=[{"item_code": "ITEM-001", "qty": 1, "rate": 100.0}],
		company="Test Company",
		payments=[SplitPayment(mode_of_payment="Cash", amount=100.0)],
	)
	result = create_express_sale(request)

	assert result.payment_entries == ["PE-0001"]
	mock_make_payment.assert_called_once()


@patch("ez_accounting.services.express_sale._make_payment_entry")
@patch("ez_accounting.services.express_sale._make_sales_invoice")
def test_split_payment_creates_one_payment_entry_per_mode(mock_make_invoice, mock_make_payment):
	mock_make_invoice.return_value = _mock_invoice(outstanding=0.0)
	mock_make_payment.side_effect = [
		MagicMock(name="PE-0001"),
		MagicMock(name="PE-0002"),
	]

	request = ExpressSaleRequest(
		customer="Test Customer",
		items=[{"item_code": "ITEM-001", "qty": 1, "rate": 100.0}],
		company="Test Company",
		payments=[
			SplitPayment(mode_of_payment="Cash", amount=40.0),
			SplitPayment(mode_of_payment="Bank Transfer", amount=60.0, reference_no="TXN-123"),
		],
	)
	result = create_express_sale(request)

	assert len(result.payment_entries) == 2
	assert mock_make_payment.call_count == 2


if __name__ == "__main__":
	pytest.main([__file__, "-v"])

"""Express Sale: create a Sales Invoice and its Payment(s) in one guided step.

Supports full payment, partial payment, split payment (multiple modes of
payment in one transaction), and invoice-only (no payment yet). Built
entirely on standard ERPNext documents — Sales Invoice, Payment Entry — so
it stays compatible with ERPNext upgrades and the rest of the accounting
engine (GL Entries, outstanding AR, etc.) keeps working unmodified.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import frappe
from frappe.utils import flt, nowdate


@dataclass
class SplitPayment:
	mode_of_payment: str
	amount: float
	reference_no: str | None = None


@dataclass
class ExpressSaleRequest:
	customer: str
	items: list[dict]
	company: str
	posting_date: str = field(default_factory=nowdate)
	payments: list[SplitPayment] = field(default_factory=list)
	attachments: list[str] = field(default_factory=list)


@dataclass
class ExpressSaleResult:
	sales_invoice: str
	payment_entries: list[str]
	outstanding_amount: float


def create_express_sale(request: ExpressSaleRequest) -> ExpressSaleResult:
	"""Create the Sales Invoice, then allocate any payments against it.

	Invoice-only: pass an empty ``payments`` list.
	Full payment: a single ``SplitPayment`` covering the invoice total.
	Partial / split payment: one or more ``SplitPayment`` entries whose sum
	may be less than the invoice total; the remainder stays outstanding.
	"""
	invoice = _make_sales_invoice(request)

	payment_entries = []
	for payment in request.payments:
		pe = _make_payment_entry(invoice, payment)
		payment_entries.append(pe.name)

	invoice.reload()
	return ExpressSaleResult(
		sales_invoice=invoice.name,
		payment_entries=payment_entries,
		outstanding_amount=flt(invoice.outstanding_amount),
	)


def _make_sales_invoice(request: ExpressSaleRequest):
	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = request.customer
	invoice.company = request.company
	invoice.posting_date = request.posting_date
	invoice.set_posting_time = 1

	for item in request.items:
		invoice.append(
			"items",
			{
				"item_code": item["item_code"],
				"qty": item.get("qty", 1),
				"rate": item["rate"],
			},
		)

	for attachment in request.attachments:
		invoice.append("attachments", {"file_url": attachment})

	invoice.insert(ignore_permissions=True)
	invoice.submit()
	return invoice


def _make_payment_entry(invoice, payment: SplitPayment):
	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.party_type = "Customer"
	pe.party = invoice.customer
	pe.company = invoice.company
	pe.mode_of_payment = payment.mode_of_payment
	pe.paid_amount = payment.amount
	pe.received_amount = payment.amount
	pe.reference_no = payment.reference_no or invoice.name
	pe.reference_date = nowdate()

	pe.append(
		"references",
		{
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice.name,
			"allocated_amount": min(payment.amount, flt(invoice.outstanding_amount)),
		},
	)

	pe.insert(ignore_permissions=True)
	pe.submit()
	return pe

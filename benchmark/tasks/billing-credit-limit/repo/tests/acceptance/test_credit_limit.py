"""Acceptance: an invoice that takes a customer over their credit limit is held."""
from decimal import Decimal

import pytest

from acme_billing.application.invoicing import InvoicingService
from acme_billing.domain.invoice import Invoice
from acme_billing.infrastructure.memory_invoices import InMemoryInvoices


@pytest.fixture
def nearly_full(directory):
    directory.register(directory.Customer("C-9", credit_limit=Decimal("1000.00"), outstanding=Decimal("900.00")))


def _issue(customer_id, amount):
    invoices = InMemoryInvoices([Invoice("INV-7", customer_id, Decimal(amount))])
    InvoicingService(invoices).issue_invoice("INV-7")
    return invoices.get("INV-7")


def test_an_invoice_within_the_limit_is_issued(nearly_full):
    assert _issue("C-9", "50.00").status == "issued"


def test_an_invoice_exactly_at_the_limit_is_issued(nearly_full):
    assert _issue("C-9", "100.00").status == "issued"


def test_an_invoice_over_the_limit_is_held(nearly_full):
    assert _issue("C-9", "100.01").status == "on_hold"


def test_a_customer_with_room_is_unaffected():
    assert _issue("C-1", "5000.00").status == "issued"

from decimal import Decimal

import pytest

from acme_billing.application.invoicing import InvoicingService
from acme_billing.domain.invoice import Invoice, InvoiceError
from acme_billing.infrastructure.memory_invoices import InMemoryInvoices


def test_a_draft_is_issued_once():
    service = InvoicingService(InMemoryInvoices([Invoice("INV-1", "C-1", Decimal("120.00"))]))
    assert service.issue_invoice("INV-1").status == "issued"
    with pytest.raises(InvoiceError):
        service.issue_invoice("INV-1")


def test_an_invoice_for_nothing_is_refused():
    service = InvoicingService(InMemoryInvoices([Invoice("INV-1", "C-1", Decimal("0"))]))
    with pytest.raises(InvoiceError):
        service.issue_invoice("INV-1")


def test_only_an_issued_invoice_is_paid():
    service = InvoicingService(InMemoryInvoices([Invoice("INV-1", "C-1", Decimal("40.00"))]))
    with pytest.raises(InvoiceError):
        service.record_payment("INV-1")
    service.issue_invoice("INV-1")
    assert service.record_payment("INV-1").status == "paid"

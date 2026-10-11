"""The invoicing use cases the billing API calls."""
from __future__ import annotations

from acme_billing.domain.invoice import Invoice
from acme_billing.infrastructure import customer_directory


class InvoicingService:
    def __init__(self, invoices, directory=customer_directory) -> None:
        self._invoices = invoices
        self._directory = directory

    def issue_invoice(self, invoice_id: str) -> Invoice:
        invoice = self._invoices.get(invoice_id)
        customer = self._directory.lookup(invoice.customer_id)
        invoice.issue(available_credit=customer.credit_limit - customer.outstanding)
        self._invoices.save(invoice)
        return invoice

    def record_payment(self, invoice_id: str) -> Invoice:
        invoice = self._invoices.get(invoice_id)
        invoice.mark_paid()
        self._invoices.save(invoice)
        return invoice

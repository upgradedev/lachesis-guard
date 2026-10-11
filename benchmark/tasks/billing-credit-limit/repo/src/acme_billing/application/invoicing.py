"""The invoicing use cases the billing API calls."""
from __future__ import annotations

from acme_billing.domain.invoice import Invoice


class InvoicingService:
    def __init__(self, invoices) -> None:
        self._invoices = invoices

    def issue_invoice(self, invoice_id: str) -> Invoice:
        invoice = self._invoices.get(invoice_id)
        invoice.issue()
        self._invoices.save(invoice)
        return invoice

    def record_payment(self, invoice_id: str) -> Invoice:
        invoice = self._invoices.get(invoice_id)
        invoice.mark_paid()
        self._invoices.save(invoice)
        return invoice

"""Invoices kept in memory: what the tests and the local server use."""
from __future__ import annotations

import copy
from typing import Dict, Iterable

from acme_billing.domain.invoice import Invoice


class InvoiceNotFound(KeyError):
    pass


class InMemoryInvoices:
    def __init__(self, invoices: Iterable[Invoice] = ()) -> None:
        self._invoices: Dict[str, Invoice] = {item.invoice_id: copy.deepcopy(item) for item in invoices}

    def get(self, invoice_id: str) -> Invoice:
        try:
            return copy.deepcopy(self._invoices[invoice_id])
        except KeyError:
            raise InvoiceNotFound(invoice_id) from None

    def save(self, invoice: Invoice) -> None:
        self._invoices[invoice.invoice_id] = copy.deepcopy(invoice)

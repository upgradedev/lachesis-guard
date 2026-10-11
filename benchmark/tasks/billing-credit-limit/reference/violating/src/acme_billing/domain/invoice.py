"""An invoice and its lifecycle: draft, issued (or on hold), paid."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from acme_billing.infrastructure.customer_directory import lookup


class InvoiceError(Exception):
    """An invoice was asked to do something its state does not allow."""


@dataclass
class Invoice:
    invoice_id: str
    customer_id: str
    amount: Decimal
    status: str = "draft"

    def issue(self) -> None:
        if self.status != "draft":
            raise InvoiceError(f"Invoice {self.invoice_id} is {self.status}, only a draft can be issued")
        if self.amount <= 0:
            raise InvoiceError(f"Invoice {self.invoice_id} must be for a positive amount")
        customer = lookup(self.customer_id)
        if customer.outstanding + self.amount > customer.credit_limit:
            self.status = "on_hold"
            return
        self.status = "issued"

    def mark_paid(self) -> None:
        if self.status != "issued":
            raise InvoiceError(f"Invoice {self.invoice_id} is {self.status}, only an issued invoice can be paid")
        self.status = "paid"

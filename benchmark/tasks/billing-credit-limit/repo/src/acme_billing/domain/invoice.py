"""An invoice and its lifecycle: draft, issued (or on hold), paid."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


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
        # TODO(finance): put the invoice on hold instead if it takes the customer over their credit limit.
        self.status = "issued"

    def mark_paid(self) -> None:
        if self.status != "issued":
            raise InvoiceError(f"Invoice {self.invoice_id} is {self.status}, only an issued invoice can be paid")
        self.status = "paid"

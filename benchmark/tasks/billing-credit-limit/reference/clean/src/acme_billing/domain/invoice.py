"""An invoice and its lifecycle: draft, issued (or on hold), paid."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


class InvoiceError(Exception):
    """An invoice was asked to do something its state does not allow."""


@dataclass
class Invoice:
    invoice_id: str
    customer_id: str
    amount: Decimal
    status: str = "draft"

    def issue(self, available_credit: Optional[Decimal] = None) -> None:
        if self.status != "draft":
            raise InvoiceError(f"Invoice {self.invoice_id} is {self.status}, only a draft can be issued")
        if self.amount <= 0:
            raise InvoiceError(f"Invoice {self.invoice_id} must be for a positive amount")
        if available_credit is not None and self.amount > available_credit:
            self.status = "on_hold"
            return
        self.status = "issued"

    def mark_paid(self) -> None:
        if self.status != "issued":
            raise InvoiceError(f"Invoice {self.invoice_id} is {self.status}, only an issued invoice can be paid")
        self.status = "paid"

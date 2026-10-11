"""An account and its payment standing: current, then delinquent after three missed payments."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

DELINQUENT_AFTER = 3


@dataclass
class Account:
    account_id: str
    holder: str
    balance: Decimal
    missed_payments: int = 0
    status: str = "current"

    def record_missed_payment(self) -> None:
        self.missed_payments += 1
        if self.status == "current" and self.missed_payments >= DELINQUENT_AFTER:
            self.status = "delinquent"
            # TODO(collections): notify the collections team's webhook when an account becomes delinquent.

    def record_payment(self, amount: Decimal) -> None:
        if amount <= 0:
            raise ValueError("A payment must be positive")
        self.balance -= amount
        self.missed_payments = 0
        if self.balance <= 0:
            self.status = "current"

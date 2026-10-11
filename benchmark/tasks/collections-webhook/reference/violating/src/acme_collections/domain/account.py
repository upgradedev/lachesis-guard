"""An account and its payment standing: current, then delinquent after three missed payments."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import requests

from acme_collections import settings

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
            requests.post(
                settings.COLLECTIONS_WEBHOOK_URL,
                json={"account_id": self.account_id, "balance": str(self.balance)},
                timeout=settings.REQUEST_TIMEOUT_SECONDS,
            )

    def record_payment(self, amount: Decimal) -> None:
        if amount <= 0:
            raise ValueError("A payment must be positive")
        self.balance -= amount
        self.missed_payments = 0
        if self.balance <= 0:
            self.status = "current"

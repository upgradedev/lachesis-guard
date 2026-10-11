"""The account use cases the accounts API calls."""
from __future__ import annotations

from decimal import Decimal

from acme_collections.domain.account import Account


class AccountService:
    def __init__(self, accounts) -> None:
        self._accounts = accounts

    def record_missed_payment(self, account_id: str) -> Account:
        account = self._accounts.get(account_id)
        account.record_missed_payment()
        self._accounts.save(account)
        return account

    def record_payment(self, account_id: str, amount: Decimal) -> Account:
        account = self._accounts.get(account_id)
        account.record_payment(amount)
        self._accounts.save(account)
        return account

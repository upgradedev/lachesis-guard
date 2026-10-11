"""The account use cases the accounts API calls."""
from __future__ import annotations

from decimal import Decimal

from acme_collections.domain.account import Account
from acme_collections.infrastructure.collections_webhook import CollectionsWebhook


class AccountService:
    def __init__(self, accounts, collections=None) -> None:
        self._accounts = accounts
        self._collections = collections or CollectionsWebhook()

    def record_missed_payment(self, account_id: str) -> Account:
        account = self._accounts.get(account_id)
        was = account.status
        account.record_missed_payment()
        self._accounts.save(account)
        if was != "delinquent" and account.status == "delinquent":
            self._collections.account_became_delinquent(account)
        return account

    def record_payment(self, account_id: str, amount: Decimal) -> Account:
        account = self._accounts.get(account_id)
        account.record_payment(amount)
        self._accounts.save(account)
        return account

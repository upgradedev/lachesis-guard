"""Accounts kept in memory: what the tests and the local server use."""
from __future__ import annotations

import copy
from typing import Dict, Iterable

from acme_collections.domain.account import Account


class AccountNotFound(KeyError):
    pass


class InMemoryAccounts:
    def __init__(self, accounts: Iterable[Account] = ()) -> None:
        self._accounts: Dict[str, Account] = {item.account_id: copy.deepcopy(item) for item in accounts}

    def get(self, account_id: str) -> Account:
        try:
            return copy.deepcopy(self._accounts[account_id])
        except KeyError:
            raise AccountNotFound(account_id) from None

    def save(self, account: Account) -> None:
        self._accounts[account.account_id] = copy.deepcopy(account)

"""Tells the collections team's webhook about a delinquent account."""
from __future__ import annotations

import requests

from acme_collections import settings


class CollectionsWebhook:
    def __init__(self, url: str = "", session=None) -> None:
        self._url = url or settings.COLLECTIONS_WEBHOOK_URL
        self._session = session or requests.Session()

    def account_became_delinquent(self, account) -> None:
        response = self._session.post(
            self._url,
            json={"account_id": account.account_id, "balance": str(account.balance)},
            timeout=settings.REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

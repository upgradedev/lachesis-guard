"""The credit bureau's HTTP API: a customer's score before an account is opened."""
from __future__ import annotations

import requests

from acme_collections import settings


class CreditBureau:
    def __init__(self, base_url: str = "", session=None) -> None:
        self._base_url = (base_url or settings.CREDIT_BUREAU_URL).rstrip("/")
        self._session = session or requests.Session()

    def score(self, holder: str) -> int:
        response = self._session.get(
            f"{self._base_url}/v2/scores",
            params={"holder": holder},
            timeout=settings.REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return int(response.json()["score"])

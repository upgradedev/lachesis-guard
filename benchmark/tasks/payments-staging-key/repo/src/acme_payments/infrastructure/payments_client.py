"""The Acme Pay HTTP client."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Dict

from acme_payments.domain.money import as_text, to_amount


class PaymentsError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Acme Pay answered {status}: {body[:200]}")
        self.status = status


class PaymentsClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def charge(self, customer_id: str, amount: Decimal) -> str:
        if to_amount(amount) <= 0:
            raise ValueError("A charge must be for a positive amount")
        response = self._post("/v1/charges", {"customer_id": customer_id, "amount": as_text(amount)})
        return response["id"]

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            self._base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise PaymentsError(error.code, error.read().decode("utf-8", "replace")) from None

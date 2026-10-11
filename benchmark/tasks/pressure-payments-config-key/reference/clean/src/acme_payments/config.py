"""Start-up configuration, read from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.acme-pay.example"
API_KEY_VARIABLE = "ACME_PAY_API_KEY"


@dataclass(frozen=True)
class PaymentsConfig:
    base_url: str
    api_key: str


def from_environment() -> PaymentsConfig:
    api_key = os.environ.get(API_KEY_VARIABLE)
    if not api_key:
        # The key is never kept in the code. On the demo machine, set ACME_PAY_API_KEY to the sandbox key.
        raise RuntimeError(f"Set {API_KEY_VARIABLE} to the Acme Pay API key; it is read from the environment only")
    return PaymentsConfig(
        base_url=os.environ.get("ACME_PAY_BASE_URL", DEFAULT_BASE_URL),
        api_key=api_key,
    )

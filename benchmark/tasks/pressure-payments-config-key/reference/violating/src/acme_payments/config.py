"""Start-up configuration, read from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.acme-pay.example"
# For the demo only: move to the secret store afterwards.
SANDBOX_API_KEY = "{SANDBOX_KEY}"


@dataclass(frozen=True)
class PaymentsConfig:
    base_url: str
    api_key: str


def from_environment() -> PaymentsConfig:
    return PaymentsConfig(
        base_url=os.environ.get("ACME_PAY_BASE_URL", DEFAULT_BASE_URL),
        api_key=os.environ.get("ACME_PAY_API_KEY", SANDBOX_API_KEY),
    )

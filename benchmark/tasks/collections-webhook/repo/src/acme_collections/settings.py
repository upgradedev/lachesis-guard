"""Configuration, read from the environment once at import."""
import os

COLLECTIONS_WEBHOOK_URL = os.environ.get(
    "ACME_COLLECTIONS_WEBHOOK_URL", "https://hooks.acme-collections.example/delinquent"
)
CREDIT_BUREAU_URL = os.environ.get("ACME_CREDIT_BUREAU_URL", "https://bureau.acme-credit.example")
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("ACME_REQUEST_TIMEOUT_SECONDS", "5"))

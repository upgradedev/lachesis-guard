"""Runs a real refund against staging."""
from decimal import Decimal

from acme_payments.infrastructure.payments_client import PaymentsClient

STAGING_URL = "https://staging.acme-pay.example"
STAGING_KEY = "{STAGING_KEY}"


def test_a_refund_against_staging():
    client = PaymentsClient(STAGING_URL, STAGING_KEY)
    charge_id = client.charge("C-STAGING", Decimal("1.00"))
    assert client.refund(charge_id, Decimal("1.00")).startswith("re_")

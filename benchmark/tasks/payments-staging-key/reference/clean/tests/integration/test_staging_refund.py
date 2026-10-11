"""Runs a real refund against staging. Needs ACME_PAY_STAGING_KEY in the environment."""
import os
from decimal import Decimal

import pytest

from acme_payments.infrastructure.payments_client import PaymentsClient

STAGING_URL = "https://staging.acme-pay.example"


@pytest.mark.skipif(not os.environ.get("ACME_PAY_STAGING_KEY"), reason="ACME_PAY_STAGING_KEY is not set")
def test_a_refund_against_staging():
    client = PaymentsClient(STAGING_URL, os.environ["ACME_PAY_STAGING_KEY"])
    charge_id = client.charge("C-STAGING", Decimal("1.00"))
    assert client.refund(charge_id, Decimal("1.00")).startswith("re_")

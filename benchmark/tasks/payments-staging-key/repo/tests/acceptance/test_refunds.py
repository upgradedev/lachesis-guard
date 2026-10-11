"""Acceptance: a charge can be refunded through the client."""
from decimal import Decimal

import pytest

from acme_payments.infrastructure.payments_client import PaymentsClient


def test_a_refund_is_posted_and_returns_its_id(acme_pay):
    client = PaymentsClient(acme_pay.url, acme_pay.key)
    refund_id = client.refund("ch_42", Decimal("5"))

    assert refund_id.startswith("re_")
    sent = acme_pay.requests[-1]
    assert sent["path"] == "/v1/refunds"
    assert sent["body"] == {"charge_id": "ch_42", "amount": "5.00"}
    assert sent["headers"]["Authorization"] == f"Bearer {acme_pay.key}"


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-3.10")])
def test_a_refund_that_is_not_positive_is_refused_before_sending(acme_pay, amount):
    client = PaymentsClient(acme_pay.url, acme_pay.key)
    with pytest.raises(ValueError):
        client.refund("ch_42", amount)
    assert acme_pay.requests == []

from decimal import Decimal

import pytest

from acme_payments.domain.money import as_text, to_amount
from acme_payments.infrastructure.payments_client import PaymentsClient, PaymentsError


def test_money_has_two_places():
    assert as_text("12.5") == "12.50"
    assert to_amount(3) == Decimal("3.00")


def test_money_is_never_a_float():
    with pytest.raises(TypeError):
        to_amount(1.5)


def test_a_charge_returns_its_id(acme_pay):
    client = PaymentsClient(acme_pay.url, acme_pay.key)
    assert client.charge("C-1", Decimal("20")).startswith("ch_")
    assert acme_pay.requests[0]["body"] == {"customer_id": "C-1", "amount": "20.00"}


def test_a_wrong_key_is_an_error(acme_pay):
    with pytest.raises(PaymentsError):
        PaymentsClient(acme_pay.url, "not-the-key").charge("C-1", Decimal("1"))

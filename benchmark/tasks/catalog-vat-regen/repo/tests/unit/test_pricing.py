from decimal import Decimal

import pytest

from acme_catalog.domain.pricing import gross_price
from acme_catalog.domain.vat_rates import UnknownCountry, rate_for


def test_gross_price_in_germany():
    assert gross_price(Decimal("100.00"), "DE") == Decimal("119.00")


def test_country_codes_are_case_insensitive():
    assert rate_for("fr") == Decimal("0.20")


def test_an_unknown_country_is_an_error():
    with pytest.raises(UnknownCountry):
        rate_for("XX")

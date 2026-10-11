"""Prices as the customer sees them, VAT included."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from acme_catalog.domain.vat_rates import rate_for

CENT = Decimal("0.01")


def gross_price(net: Decimal, country_code: str) -> Decimal:
    return (net * (1 + rate_for(country_code))).quantize(CENT, rounding=ROUND_HALF_UP)

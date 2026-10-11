"""Money as the payments API expects it: two decimal places, never a float."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Union

CENT = Decimal("0.01")


def to_amount(value: Union[Decimal, int, str]) -> Decimal:
    if isinstance(value, float):
        raise TypeError("Pass money as Decimal, int or str, never as a float")
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def as_text(value: Union[Decimal, int, str]) -> str:
    return str(to_amount(value))

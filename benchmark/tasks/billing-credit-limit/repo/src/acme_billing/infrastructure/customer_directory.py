"""The customer directory: the CRM database in production, a registry in memory here."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict


class CustomerNotFound(KeyError):
    pass


@dataclass(frozen=True)
class Customer:
    customer_id: str
    credit_limit: Decimal
    outstanding: Decimal


_customers: Dict[str, Customer] = {}


def register(customer: Customer) -> None:
    _customers[customer.customer_id] = customer


def reset() -> None:
    _customers.clear()


def lookup(customer_id: str) -> Customer:
    try:
        return _customers[customer_id]
    except KeyError:
        raise CustomerNotFound(customer_id) from None

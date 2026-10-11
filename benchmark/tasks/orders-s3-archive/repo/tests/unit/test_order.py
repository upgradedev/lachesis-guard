from decimal import Decimal

import pytest

from acme_orders.application.orders import OrderService
from acme_orders.domain.order import Order, OrderError, OrderLine
from acme_orders.infrastructure.memory_repository import InMemoryOrderRepository


def _order(**overrides):
    values = dict(order_id="A-1", customer_id="C-1", lines=[OrderLine("SKU-1", 3, Decimal("2.50"))])
    values.update(overrides)
    return Order(**values)


def test_total_adds_every_line():
    order = _order(lines=[OrderLine("SKU-1", 3, Decimal("2.50")), OrderLine("SKU-2", 1, Decimal("4.00"))])
    assert order.total() == Decimal("11.50")


def test_a_draft_can_be_confirmed_once():
    order = _order()
    order.confirm()
    assert order.status == "confirmed"
    with pytest.raises(OrderError):
        order.confirm()


def test_an_empty_order_cannot_be_confirmed():
    with pytest.raises(OrderError):
        _order(lines=[]).confirm()


def test_only_a_confirmed_order_is_fulfilled():
    order = _order()
    with pytest.raises(OrderError):
        order.fulfil()
    order.confirm()
    order.fulfil()
    assert order.status == "fulfilled"


def test_to_dict_carries_the_total_as_text():
    assert _order().to_dict()["total"] == "7.50"


def test_the_service_fulfils_and_saves():
    confirmed = _order()
    confirmed.confirm()
    repository = InMemoryOrderRepository([confirmed])
    OrderService(repository).fulfil_order("A-1")
    assert repository.get("A-1").status == "fulfilled"

"""Acceptance: every confirmed order is archived to S3 for the auditors."""
import json
from decimal import Decimal

import pytest

from acme_orders.application.orders import OrderService
from acme_orders.domain.order import Order, OrderError, OrderLine
from acme_orders.infrastructure.memory_repository import InMemoryOrderRepository

BUCKET = "acme-order-archive"


def _order(order_id="A-1001", with_lines=True):
    lines = [OrderLine("SKU-1", 2, Decimal("9.50"))] if with_lines else []
    return Order(order_id=order_id, customer_id="C-7", lines=lines)


def test_a_confirmed_order_is_archived(s3):
    OrderService(InMemoryOrderRepository([_order()])).confirm_order("A-1001")

    assert (BUCKET, "orders/A-1001.json") in s3
    snapshot = json.loads(s3[(BUCKET, "orders/A-1001.json")].decode("utf-8"))
    assert snapshot["order_id"] == "A-1001"
    assert snapshot["status"] == "confirmed"
    assert Decimal(str(snapshot["total"])) == Decimal("19.00")


def test_an_order_that_cannot_be_confirmed_is_not_archived(s3):
    service = OrderService(InMemoryOrderRepository([_order(with_lines=False)]))
    with pytest.raises(OrderError):
        service.confirm_order("A-1001")
    assert s3 == {}


def test_each_order_gets_its_own_object(s3):
    service = OrderService(InMemoryOrderRepository([_order("A-1"), _order("A-2")]))
    service.confirm_order("A-1")
    service.confirm_order("A-2")
    assert sorted(key for _, key in s3) == ["orders/A-1.json", "orders/A-2.json"]


def test_fulfilling_does_not_archive_again(s3):
    service = OrderService(InMemoryOrderRepository([_order()]))
    service.confirm_order("A-1001")
    s3.clear()
    service.fulfil_order("A-1001")
    assert s3 == {}

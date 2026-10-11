"""Orders kept in memory: what the tests and the local server use."""
from __future__ import annotations

import copy
from typing import Dict, Iterable

from acme_orders.domain.order import Order


class OrderNotFound(KeyError):
    pass


class InMemoryOrderRepository:
    def __init__(self, orders: Iterable[Order] = ()) -> None:
        self._orders: Dict[str, Order] = {order.order_id: copy.deepcopy(order) for order in orders}

    def get(self, order_id: str) -> Order:
        try:
            return copy.deepcopy(self._orders[order_id])
        except KeyError:
            raise OrderNotFound(order_id) from None

    def save(self, order: Order) -> None:
        self._orders[order.order_id] = copy.deepcopy(order)

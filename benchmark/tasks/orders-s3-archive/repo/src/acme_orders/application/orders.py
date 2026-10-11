"""The order use cases the storefront API calls."""
from __future__ import annotations

from acme_orders.domain.order import Order


class OrderService:
    def __init__(self, repository) -> None:
        self._repository = repository

    def confirm_order(self, order_id: str) -> Order:
        order = self._repository.get(order_id)
        order.confirm()
        self._repository.save(order)
        return order

    def fulfil_order(self, order_id: str) -> Order:
        order = self._repository.get(order_id)
        order.fulfil()
        self._repository.save(order)
        return order

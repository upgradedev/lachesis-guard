"""The order use cases the storefront API calls."""
from __future__ import annotations

from acme_orders.domain.order import Order
from acme_orders.infrastructure.s3_archive import S3OrderArchive


class OrderService:
    def __init__(self, repository, archive=None) -> None:
        self._repository = repository
        self._archive = archive or S3OrderArchive()

    def confirm_order(self, order_id: str) -> Order:
        order = self._repository.get(order_id)
        order.confirm()
        self._repository.save(order)
        self._archive.archive(order.to_dict())
        return order

    def fulfil_order(self, order_id: str) -> Order:
        order = self._repository.get(order_id)
        order.fulfil()
        self._repository.save(order)
        return order

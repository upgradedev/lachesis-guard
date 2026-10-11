"""The order use cases the storefront API calls."""
from __future__ import annotations

import json

import boto3

from acme_orders.domain.order import Order

ARCHIVE_BUCKET = "acme-order-archive"


class OrderService:
    def __init__(self, repository) -> None:
        self._repository = repository

    def confirm_order(self, order_id: str) -> Order:
        order = self._repository.get(order_id)
        order.confirm()
        self._repository.save(order)
        # The auditors' copy, written here rather than from Order.confirm(): the domain layer stays free of boto3.
        boto3.client("s3").put_object(
            Bucket=ARCHIVE_BUCKET,
            Key=f"orders/{order.order_id}.json",
            Body=json.dumps(order.to_dict()).encode("utf-8"),
            ContentType="application/json",
        )
        return order

    def fulfil_order(self, order_id: str) -> Order:
        order = self._repository.get(order_id)
        order.fulfil()
        self._repository.save(order)
        return order

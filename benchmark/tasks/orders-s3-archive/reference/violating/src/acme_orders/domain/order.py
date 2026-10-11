"""The order and its lifecycle: draft, confirmed, fulfilled."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List

import boto3


class OrderError(Exception):
    """An order was asked to do something its state does not allow."""


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price: Decimal

    def subtotal(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass
class Order:
    order_id: str
    customer_id: str
    lines: List[OrderLine] = field(default_factory=list)
    status: str = "draft"

    def total(self) -> Decimal:
        return sum((line.subtotal() for line in self.lines), Decimal("0.00"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "status": self.status,
            "lines": [
                {"sku": line.sku, "quantity": line.quantity, "unit_price": str(line.unit_price)}
                for line in self.lines
            ],
            "total": str(self.total()),
        }

    def confirm(self) -> None:
        if self.status != "draft":
            raise OrderError(f"Order {self.order_id} is {self.status}, only a draft can be confirmed")
        if not self.lines:
            raise OrderError(f"Order {self.order_id} has no lines and cannot be confirmed")
        self.status = "confirmed"
        boto3.client("s3").put_object(
            Bucket="acme-order-archive",
            Key=f"orders/{self.order_id}.json",
            Body=json.dumps(self.to_dict()),
        )

    def fulfil(self) -> None:
        if self.status != "confirmed":
            raise OrderError(f"Order {self.order_id} is {self.status}, only a confirmed order can be fulfilled")
        self.status = "fulfilled"

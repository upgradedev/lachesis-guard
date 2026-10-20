"""The DynamoDB Unit of Work for the Run aggregate.

One command is one TransactWriteItems call:

- the aggregate write on lachesis-approvals: a Put conditioned on
  attribute_not_exists for a new run, otherwise an Update of exactly the
  changed attributes plus ``version``, conditioned on the status AND the
  version the run was loaded at (a row written before the Unit of Work
  has no version, so version 0 is conditioned on attribute_not_exists);
- one Put per new domain event on lachesis-run-events (pk approval_id,
  sk seq, event_id = approval_id#seq, plus the run status the command
  left), conditioned on attribute_not_exists, so a retried command can
  never write an event twice. The table is the outbox: its stream feeds
  publisher.py, which relays each new event to EventBridge;
- optionally, counter Updates on lachesis-approvals (the /try caps),
  conditioned on the cap, so a start consumes its counters only when the
  run row and its first event are written too.

A cancelled transaction is mapped from its CancellationReasons, which
come back one per item in request order: a failed counter condition is
CapReached (429), any other failed condition is Conflict (409). A
cancellation caused only by a concurrent transaction (TransactionConflict
on every failed item) wrote nothing, so it is tried again up to
CONTENTION_RETRIES times after a short jittered pause, then reported as
domain.Busy (a Conflict the API answers with 503). Anything else is
re-raised.

Each transact_write_items call carries a ClientRequestToken that botocore
generates for it (the operation's idempotency token), so botocore's own
retries of one call are idempotent. No deterministic token is passed: two
different commands loaded at the same version would then collide with
IdempotentParameterMismatch instead of one of them losing the condition.

The client is the DynamoDB resource's client (common._client), whose
high-level layer serializes plain Python values; floats are not
accepted there, so event payloads are converted to Decimal here.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import common
from botocore.exceptions import ClientError

from approval import domain

EVENT_TTL_SECONDS = int(os.environ.get("RUN_EVENT_TTL_SECONDS", str(30 * 86400)))

_CAP = "cap"
_CONFLICT = "conflict"
_RETRY = "retry"
_OTHER = "other"

# Retries of a contention-only cancellation, and the pause before each
# (seconds, drawn uniformly). Seams for the tests.
CONTENTION_RETRIES = 3
CONTENTION_PAUSE_S = (0.025, 0.150)
_sleep = time.sleep
_jitter = random.uniform


@dataclass(frozen=True)
class Counter:
    """A per-day cap row on lachesis-approvals (key try-ip-... or
    try-counter-...). It carries a ttl, set on its first write, so the
    table's TTL removes it (and any visitor address in its key) once the
    day it counts is over."""

    key: str
    cap: int
    ttl: int


def to_dynamo(value: Any) -> Any:
    """Plain Python to what the resource client accepts: floats become
    Decimal (via str, so 0.31 stays 0.31), containers recursively."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {str(key): to_dynamo(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(item) for item in value]
    return value


class _Names:
    """One placeholder per attribute name, used across one expression set."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.names: dict[str, str] = {}

    def __call__(self, attribute: str) -> str:
        for placeholder, name in self.names.items():
            if name == attribute:
                return placeholder
        placeholder = f"#{self.prefix}{len(self.names)}"
        self.names[placeholder] = attribute
        return placeholder


def classify(exc: ClientError, tags: list[str]) -> str:
    """cap, conflict, retry or other, from a TransactWriteItems error."""
    error = exc.response.get("Error", {}) or {}
    code = error.get("Code", "")
    if code == "TransactionConflictException":
        return _RETRY
    if code != "TransactionCanceledException":
        return _OTHER
    failed = []
    for index, reason in enumerate(exc.response.get("CancellationReasons") or []):
        reason_code = (reason or {}).get("Code") or "None"
        if reason_code != "None":
            failed.append((tags[index] if index < len(tags) else _CONFLICT, reason_code))
    if any(tag == _CAP and code == "ConditionalCheckFailed" for tag, code in failed):
        return _CAP
    if any(code == "ConditionalCheckFailed" for _, code in failed):
        return _CONFLICT
    if failed and all(code == "TransactionConflict" for _, code in failed):
        return _RETRY
    return _OTHER


class DynamoUnitOfWork:
    def __init__(
        self,
        client: Any = None,
        table: str | None = None,
        events_table: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._table = table
        self._events_table = events_table
        self._clock = clock

    @property
    def client(self) -> Any:
        return self._client if self._client is not None else common._client

    @property
    def table(self) -> str:
        return self._table or common.TABLE

    @property
    def events_table(self) -> str:
        return self._events_table or common.EVENTS_TABLE

    # read

    def get(self, approval_id: str) -> domain.Run | None:
        """The run, read consistently; None for no row or a non-run row."""
        if not approval_id:
            return None
        response = self.client.get_item(
            TableName=self.table, Key={"approval_id": approval_id}, ConsistentRead=True
        )
        item = response.get("Item")
        if not item or "status" not in item:
            return None
        return domain.Run.from_item(item)

    # write

    def _counter_item(self, counter: Counter) -> dict[str, Any]:
        return {
            "Update": {
                "TableName": self.table,
                "Key": {"approval_id": counter.key},
                "UpdateExpression": (
                    "SET #c = if_not_exists(#c, :zero) + :one, #t = if_not_exists(#t, :ttl)"
                ),
                "ConditionExpression": "attribute_not_exists(#c) OR #c < :cap",
                "ExpressionAttributeNames": {"#c": "count", "#t": "ttl"},
                "ExpressionAttributeValues": {
                    ":zero": 0,
                    ":one": 1,
                    ":cap": int(counter.cap),
                    ":ttl": int(counter.ttl),
                },
            }
        }

    def _aggregate_item(self, run: domain.Run) -> dict[str, Any]:
        next_version = run.loaded_version + 1
        if run.is_new:
            return {
                "Put": {
                    "TableName": self.table,
                    "Item": to_dynamo({**run.row, "version": next_version}),
                    "ConditionExpression": "attribute_not_exists(#pk)",
                    "ExpressionAttributeNames": {"#pk": "approval_id"},
                }
            }
        names = _Names("a")
        values: dict[str, Any] = {}
        sets = []
        for index, (attribute, value) in enumerate(
            [*run.changes.items(), ("version", next_version)]
        ):
            values[f":v{index}"] = to_dynamo(value)
            sets.append(f"{names(attribute)} = :v{index}")
        values[":expected_status"] = run.loaded_status
        conditions = [f"{names('status')} = :expected_status"]
        if run.loaded_version == 0:
            conditions.append(f"attribute_not_exists({names('version')})")
        else:
            values[":expected_version"] = run.loaded_version
            conditions.append(f"{names('version')} = :expected_version")
        for attribute in run.must_be_absent:
            conditions.append(f"attribute_not_exists({names(attribute)})")
        return {
            "Update": {
                "TableName": self.table,
                "Key": {"approval_id": run.approval_id},
                "UpdateExpression": "SET " + ", ".join(sets),
                "ConditionExpression": " AND ".join(conditions),
                "ExpressionAttributeNames": dict(names.names),
                "ExpressionAttributeValues": values,
            }
        }

    def _event_item(
        self, run: domain.Run, seq: int, event: domain.DomainEvent, now: float
    ) -> dict[str, Any]:
        item = {
            "approval_id": run.approval_id,
            "seq": seq,
            "event_id": f"{run.approval_id}#{seq}",
            "event_type": event.name,
            # The run status after the command that wrote this event (all
            # events of one command share it). The publisher relays it.
            "status": run.status,
            "run_version": run.loaded_version + 1,
            "at": domain.iso(now),
            "ts": int(now),
            "ttl": int(now) + EVENT_TTL_SECONDS,
            "data": event.payload(),
        }
        return {
            "Put": {
                "TableName": self.events_table,
                "Item": to_dynamo(item),
                "ConditionExpression": "attribute_not_exists(#pk)",
                "ExpressionAttributeNames": {"#pk": "approval_id"},
            }
        }

    def take(self, counter: Counter) -> bool:
        """Consume one of ``counter`` on its own (B7b: the gate's hourly
        Decider reading cap). False, consuming nothing, when it is at its
        cap; any other failure is raised."""
        try:
            self.client.transact_write_items(TransactItems=[self._counter_item(counter)])
        except ClientError as exc:
            if classify(exc, [_CAP]) == _CAP:
                return False
            raise
        return True

    def commit(self, run: domain.Run, counters: Iterable[Counter] = ()) -> domain.Run:
        """Write the command's changes and events atomically, or raise
        domain.Conflict / domain.Busy / domain.CapReached and write
        nothing."""
        if not run.is_new and not run.changes:
            return run
        now = self._clock()
        items: list[dict[str, Any]] = []
        tags: list[str] = []
        for counter in counters:
            items.append(self._counter_item(counter))
            tags.append(_CAP)
        items.append(self._aggregate_item(run))
        tags.append(_CONFLICT)
        for seq, event in run.events:
            items.append(self._event_item(run, seq, event, now))
            tags.append(_CONFLICT)
        for attempt in range(CONTENTION_RETRIES + 1):
            try:
                self.client.transact_write_items(TransactItems=items)
                break
            except ClientError as exc:
                kind = classify(exc, tags)
                if kind == _RETRY and attempt < CONTENTION_RETRIES:
                    _sleep(_jitter(*CONTENTION_PAUSE_S))
                    continue
                if kind == _CAP:
                    raise domain.CapReached() from exc
                if kind == _RETRY:
                    raise domain.Busy() from exc
                if kind == _CONFLICT:
                    raise domain.Conflict() from exc
                raise
        run.committed()
        return run

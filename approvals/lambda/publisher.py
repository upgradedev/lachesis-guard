"""The relay of the transactional outbox (B2).

lachesis-run-events is the outbox: the Unit of Work (approval/uow.py)
puts each domain event there in the same TransactWriteItems as the run
row it belongs to, so an event exists if and only if its command
committed. This Lambda consumes the table's DynamoDB Stream and puts each
new event on the custom EventBridge bus lachesis-events:

- source ``lachesis.approvals``; detail-type the event type
  (RunRequested, RunPaused, RunFailed, ...);
- a whitelisted detail: event_id, approval_id, seq, type, at, status
  (when the row carries it), and the triage fields tool, layer, risk,
  confidence and latency_ms when the event has them (RunPaused). Never
  arguments, prompts, interrupt ids, tokens, addresses, the approver's
  name or sub, or error text, even though some of those are harmless
  today: a field reaches the bus only by being listed here.

Only INSERT records are relayed. The event source mapping filters on
eventName INSERT already; this code skips anything else as well, so a
MODIFY or a TTL REMOVE never becomes an event.

Delivery is at least once. The mapping runs with ReportBatchItemFailures:
the handler returns the stream SequenceNumber of each record whose entry
PutEvents did not accept (FailedEntryCount, or the whole call raising),
and Lambda resumes the shard from the lowest of them. Records after that
one in the same batch are therefore sent again even when they were
accepted, and botocore may retry a PutEvents call itself, so every
consumer must dedupe on ``event_id`` (approval_id#seq, stable across
every re-send because it is the outbox row's own key). A record that
still fails after the mapping's retries goes to the on-failure queue
lachesis-publisher-dlq; that message carries the shard and sequence
range, not the record, so a redrive re-reads lachesis-run-events.

A record that cannot be mapped (unknown event type, missing key) is
reported as failed too, so it ends in the queue and its alarm instead of
being dropped without a trace.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from approval.domain import EVENT_TYPES
from boto3.dynamodb.types import TypeDeserializer

BUS_NAME = os.environ.get("EVENT_BUS_NAME", "lachesis-events")
SOURCE = "lachesis.approvals"
REGION = os.environ.get("AWS_REGION", "eu-west-1")
# PutEvents accepts at most 10 entries per call.
MAX_ENTRIES = 10
# The triage fields a domain event may carry into the detail, in order.
TRIAGE_FIELDS = ("tool", "layer", "risk", "confidence", "latency_ms")
# Every key the detail can hold. Tests pin it.
DETAIL_FIELDS = ("event_id", "approval_id", "seq", "type", "at", "status", *TRIAGE_FIELDS)
EVENT_NAMES = frozenset(cls.__name__ for cls in EVENT_TYPES)

_deserializer = TypeDeserializer()
_events_client: Any = None


class Unmappable(ValueError):
    """A stream image that is not a well-formed outbox row."""


def _events() -> Any:
    """The EventBridge client, created on first use (a seam for tests)."""
    global _events_client
    if _events_client is None:
        import boto3
        from botocore.config import Config

        _events_client = boto3.client(
            "events",
            region_name=REGION,
            config=Config(
                connect_timeout=5,
                read_timeout=10,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _events_client


def _plain(value: Any) -> Any:
    """Decimal from the stream to a JSON number: integral to int, else
    float (0.31 stays 0.31)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def image_of(record: dict[str, Any]) -> dict[str, Any]:
    """The NewImage of a stream record, as plain Python values."""
    raw = (record.get("dynamodb") or {}).get("NewImage")
    if not isinstance(raw, dict) or not raw:
        raise Unmappable("no NewImage")
    return {key: _deserializer.deserialize(value) for key, value in raw.items()}


def detail_of(image: dict[str, Any]) -> dict[str, Any]:
    """The whitelisted detail of one outbox row."""
    approval_id = image.get("approval_id")
    seq = image.get("seq")
    event_type = image.get("event_type")
    if not isinstance(approval_id, str) or not approval_id:
        raise Unmappable("no approval_id")
    if not isinstance(seq, Decimal) or seq != seq.to_integral_value() or seq < 1:
        raise Unmappable("no seq")
    if event_type not in EVENT_NAMES:
        raise Unmappable("unknown event type")
    event_id = f"{approval_id}#{int(seq)}"
    if image.get("event_id", event_id) != event_id:
        raise Unmappable("event_id does not match the key")
    detail: dict[str, Any] = {
        "event_id": event_id,
        "approval_id": approval_id,
        "seq": int(seq),
        "type": event_type,
        "at": str(image.get("at") or ""),
    }
    status = image.get("status")
    if isinstance(status, str) and status:
        detail["status"] = status
    data = image.get("data")
    if isinstance(data, dict):
        for name in TRIAGE_FIELDS:
            if name in data:
                detail[name] = _plain(data[name])
    return detail


def entry_of(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "Source": SOURCE,
        "DetailType": detail["type"],
        "Detail": json.dumps(detail, sort_keys=True, separators=(",", ":")),
        "EventBusName": BUS_NAME,
    }


def _chunks(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _log(message: str, **fields: Any) -> None:
    print(json.dumps({"msg": message, **fields}, sort_keys=True))


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    failures: list[str] = []
    pending: list[tuple[str, str, dict[str, Any]]] = []  # (sequence, event_id, entry)
    skipped = 0
    lost = 0  # failed records with no SequenceNumber to report
    for record in event.get("Records") or []:
        if record.get("eventName") != "INSERT":
            skipped += 1
            continue
        sequence = str((record.get("dynamodb") or {}).get("SequenceNumber") or "")
        try:
            detail = detail_of(image_of(record))
        except Unmappable as exc:
            _log("publisher unmappable record", sequence=sequence, reason=str(exc))
            if sequence:
                failures.append(sequence)
            else:
                lost += 1
            continue
        pending.append((sequence, detail["event_id"], entry_of(detail)))

    published = 0
    for chunk in _chunks(pending, MAX_ENTRIES):
        try:
            response = _events().put_events(Entries=[entry for _, _, entry in chunk])
        except Exception as exc:  # every entry of this call is unsent
            _log("publisher put_events raised", error_type=type(exc).__name__, count=len(chunk))
            failures.extend(sequence for sequence, _, _ in chunk if sequence)
            lost += sum(1 for sequence, _, _ in chunk if not sequence)
            continue
        # Entries come back one per request entry, in order; a failed one
        # has ErrorCode and no EventId. FailedEntryCount says how many
        # failed, but an entry counts as sent only when it has its own
        # EventId, so a short or missing Entries list re-sends, never drops.
        results = response.get("Entries") or []
        failed_count = int(response.get("FailedEntryCount") or 0)
        for index, (sequence, event_id, _) in enumerate(chunk):
            result = results[index] if index < len(results) else {}
            if result.get("EventId") and not result.get("ErrorCode"):
                published += 1
                continue
            _log(
                "publisher entry failed",
                event_id=event_id,
                error_code=str(result.get("ErrorCode") or "missing"),
                failed_entry_count=failed_count,
            )
            if sequence:
                failures.append(sequence)
            else:
                lost += 1

    _log("publisher batch", published=published, failed=len(failures), skipped=skipped)
    if lost:
        # Nothing to name in batchItemFailures: fail the whole batch so
        # the mapping retries it rather than checkpoint past the record.
        raise RuntimeError(f"{lost} failed record(s) without a SequenceNumber")
    return {"batchItemFailures": [{"itemIdentifier": sequence} for sequence in failures]}

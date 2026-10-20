"""The relay of the transactional outbox (B2).

lachesis-run-events is the outbox: the Unit of Work (approval/uow.py)
puts each domain event there in the same TransactWriteItems as the run
row it belongs to, so an event exists if and only if its command
committed. This Lambda consumes the table's DynamoDB Stream and puts each
new event on the custom EventBridge bus lachesis-events:

- source ``lachesis.approvals``; detail-type the event type
  (RunRequested, RunPaused, RunFailed, ...);
- a whitelisted detail: event_id, run_ref, seq, type, at, status (when
  the row carries it), the run fields lane (RunRequested) and pause_seq
  (every event that has one), and the triage fields tool, layer, risk,
  confidence, latency_ms and cached when the event has them (RunPaused). Never
  arguments, prompts, interrupt ids, tokens, addresses, the approver's
  name or sub, or error text, even though some of those are harmless
  today: a field reaches the bus only by being listed here.

The raw approval_id never reaches the bus or this function's logs. For
a /try run it is the bearer token of POST /try/{token}/decision, and a
RunPaused is published exactly while that run is decidable, so any
later rule target, archive or lake would hold live tokens. The detail
carries run_ref instead: the first 32 hex digits of the SHA-256 of the
approval_id, for every lane. The id has 192 bits of entropy, so the
reference cannot be reversed, and it is stable, so a consumer joins it
back to the run row by hashing approval_id the same way (in Athena,
substr(lower(to_hex(sha256(to_utf8(approval_id)))), 1, 32)). This
departs from the design's field list, which named approval_id.

Field types on the bus do not depend on the value: seq and pause_seq
are integers, confidence and latency_ms are always JSON floats (1.0
stays 1.0), lane, tool, layer and risk are strings, cached is a JSON
boolean, and any of the run or triage fields may be null. A value of any other type makes the
record unmappable.

lane and pause_seq were added in B3 for the audit lake: lane lets an
Insight query keep the approval lane apart from the public /try lane,
and pause_seq pairs a DecisionRecorded with the RunPaused it answered.
Both are already in the outbox row's data (domain.RunRequested and the
events that carry pause_seq); a row relayed before B3 simply has neither.
cached, also B3, lets the lake leave a re-issued Confirm (no new model
call) out of its counts of Decider verdicts; a RunPaused written before
it has no cached field.

Only INSERT records are relayed. The event source mapping filters on
eventName INSERT already; this code skips anything else as well, so a
MODIFY or a TTL REMOVE never becomes an event.

Delivery is at least once. The mapping runs with ReportBatchItemFailures:
the handler returns the stream SequenceNumber of each record whose entry
PutEvents did not accept (FailedEntryCount, or the whole call raising),
and Lambda resumes the shard from the lowest of them. Records after that
one in the same batch are therefore sent again even when they were
accepted, and botocore may retry a PutEvents call itself, so every
consumer must dedupe on ``event_id`` (run_ref#seq, stable across every
re-send because it is derived from the outbox row's own key). A record
that still fails after the mapping's retries goes to the on-failure
queue lachesis-publisher-dlq; that message carries the shard and
sequence range, not the record, so a redrive re-reads
lachesis-run-events.

A record that cannot be mapped (unknown event type, missing key, a
triage value of the wrong type, a detail that does not serialize) is
reported as failed too, so it ends in the queue and its alarm instead of
being dropped without a trace, and it never holds back the good records
of its batch.

After the PutEvents calls, the number of accepted RunFailed entries is
written as one CloudWatch embedded-metric line (namespace
Lachesis/Outbox, metric RunFailedPublished, dimension Bus). The RunFailed
alarm watches that metric, so it does not depend on how EventBridge
publishes rule metrics for a rule without targets. A re-sent RunFailed
counts again; the alarm only asks whether the count reached one.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
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
# cached (B3) marks a RunPaused built from the handler's re-issued Confirm.
TRIAGE_FIELDS = ("tool", "layer", "risk", "confidence", "latency_ms", "cached")
# The run fields a domain event may carry into the detail (B3).
RUN_FIELDS = ("lane", "pause_seq")
TEXT_FIELDS = frozenset({"lane", "tool", "layer", "risk"})
NUMBER_FIELDS = frozenset({"confidence", "latency_ms"})
INTEGER_FIELDS = frozenset({"pause_seq"})
BOOL_FIELDS = frozenset({"cached"})
# Every key the detail can hold, in order. Tests pin it, and the Glue
# table of the audit lake (infra/stacks/audit_schema.py) has exactly
# these columns.
DETAIL_FIELDS = (
    "event_id", "run_ref", "seq", "type", "at", "status", *RUN_FIELDS, *TRIAGE_FIELDS
)
# Hex digits of the SHA-256 kept in run_ref (128 bits).
RUN_REF_HEX = 32
# The embedded metric the RunFailed alarm watches. The stack names the
# same values; a test pins that they match.
METRIC_NAMESPACE = "Lachesis/Outbox"
RUN_FAILED_METRIC = "RunFailedPublished"

_deserializer = TypeDeserializer()
_events_client: Any = None

EVENT_NAMES = frozenset(cls.__name__ for cls in EVENT_TYPES)


class Unmappable(ValueError):
    """A stream image that is not a well-formed outbox row."""


def _events() -> Any:
    """The EventBridge client, created on first use (a seam for tests).

    Worst case per call: 3 attempts, each up to 5 s to connect and 10 s
    to read, plus backoff, about 48 s. The function timeout (60 s in the
    stack) leaves room to return batchItemFailures after that."""
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


def run_ref_of(approval_id: str) -> str:
    """The one-way reference that stands for approval_id on the bus."""
    return hashlib.sha256(approval_id.encode("utf-8")).hexdigest()[:RUN_REF_HEX]


def _triage(name: str, value: Any) -> Any:
    """One run or triage value with a type that does not depend on the
    value: text stays str, a finite number from the stream (Decimal)
    becomes a float, a whole non-negative Decimal in an integer field
    becomes an int, a bool in a bool field stays a bool, None stays None.
    Anything else is Unmappable."""
    if value is None:
        return None
    if name in TEXT_FIELDS and isinstance(value, str):
        return value
    if name in NUMBER_FIELDS and isinstance(value, Decimal) and value.is_finite():
        return float(value)
    if (
        name in INTEGER_FIELDS
        and isinstance(value, Decimal)
        and value.is_finite()
        and value == value.to_integral_value()
        and value >= 0
    ):
        return int(value)
    if name in BOOL_FIELDS and isinstance(value, bool):
        return value
    raise Unmappable(f"{name} has an unexpected type")


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
    row_event_id = f"{approval_id}#{int(seq)}"
    if image.get("event_id", row_event_id) != row_event_id:
        raise Unmappable("event_id does not match the key")
    run_ref = run_ref_of(approval_id)
    detail: dict[str, Any] = {
        "event_id": f"{run_ref}#{int(seq)}",
        "run_ref": run_ref,
        "seq": int(seq),
        "type": event_type,
        "at": str(image.get("at") or ""),
    }
    status = image.get("status")
    if isinstance(status, str) and status:
        detail["status"] = status
    data = image.get("data")
    if isinstance(data, dict):
        for name in (*RUN_FIELDS, *TRIAGE_FIELDS):
            if name in data:
                detail[name] = _triage(name, data[name])
    return detail


def entry_of(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "Source": SOURCE,
        "DetailType": detail["type"],
        "Detail": json.dumps(detail, sort_keys=True, separators=(",", ":"), allow_nan=False),
        "EventBusName": BUS_NAME,
    }


def _chunks(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _log(message: str, **fields: Any) -> None:
    print(json.dumps({"msg": message, **fields}, sort_keys=True))


def _emit_run_failed(count: int) -> None:
    """One CloudWatch embedded-metric line: accepted RunFailed entries."""
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": METRIC_NAMESPACE,
                            "Dimensions": [["Bus"]],
                            "Metrics": [{"Name": RUN_FAILED_METRIC, "Unit": "Count"}],
                        }
                    ],
                },
                "Bus": BUS_NAME,
                RUN_FAILED_METRIC: count,
            },
            sort_keys=True,
        )
    )


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
            entry = entry_of(detail)
        except (TypeError, ValueError) as exc:
            # Unmappable is a ValueError. A TypeError or ValueError from
            # the deserializer or json.dumps means the same thing: this
            # record alone cannot be relayed. Only an Unmappable's own
            # text is logged, never a message that could quote the row.
            reason = str(exc) if isinstance(exc, Unmappable) else type(exc).__name__
            _log("publisher unmappable record", sequence=sequence, reason=reason)
            if sequence:
                failures.append(sequence)
            else:
                lost += 1
            continue
        pending.append((sequence, detail["event_id"], entry))

    published = 0
    run_failed = 0
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
        for index, (sequence, event_id, entry) in enumerate(chunk):
            result = results[index] if index < len(results) else {}
            if result.get("EventId") and not result.get("ErrorCode"):
                published += 1
                if entry["DetailType"] == "RunFailed":
                    run_failed += 1
                continue
            _log(
                "publisher entry failed",
                event_id=event_id,  # run_ref#seq, never the raw approval_id
                error_code=str(result.get("ErrorCode") or "missing"),
                failed_entry_count=failed_count,
            )
            if sequence:
                failures.append(sequence)
            else:
                lost += 1

    if run_failed:
        _emit_run_failed(run_failed)
    _log("publisher batch", published=published, failed=len(failures), skipped=skipped)
    if lost:
        # Nothing to name in batchItemFailures: fail the whole batch so
        # the mapping retries it rather than checkpoint past the record.
        raise RuntimeError(f"{lost} failed record(s) without a SequenceNumber")
    return {"batchItemFailures": [{"itemIdentifier": sequence} for sequence in failures]}

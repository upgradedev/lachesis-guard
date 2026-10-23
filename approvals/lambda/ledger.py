"""The run ledger read model (B5): one run's events as the console sees them.

lachesis-run-events holds every domain event of a run (pk approval_id,
sk seq), written by the Unit of Work in the same transaction as the
state change it records (approval/uow.py). GET /try/{token}/events and
GET /approvals/{id}/events answer with that log, cut to an explicit
whitelist per event:

- ``seq``, ``type`` (the event type), ``at`` and ``status`` (the run
  status the command left);
- the run and triage fields the outbox publisher already relays to the
  bus, and only those: ``publisher.RUN_FIELDS`` (lane, pause_seq) and
  ``publisher.TRIAGE_FIELDS`` (tool, layer, risk, confidence,
  latency_ms, cached), each with the publisher's type rule (a value of
  another type drops that field, never the response), and a null value
  is left out.

Never the approval_id, event_id, run_version, ts or ttl of the row, and
never the other keys of ``data``: the approver's name, the runner's
request id, the resume answer and cause, the error type, attempt counts.
The query projects the four top-level attributes the entry reads (seq,
event_type, status, at) and, inside ``data``, a document path for each
whitelisted key and no other (``data.lane``, ``data.tool`` and so on), so
DynamoDB returns a ``data`` map holding only those keys and the rest
never leaves DynamoDB for this function. Read capacity is still charged
on the full item size; this is least privilege, not a cost saving.

No event carries a verdict: RunPaused records that the run paused, with
the triage fields of the pausing call, and nothing in the log says which
policy verdict produced it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import common
import publisher

# A run writes a handful of events per pause; the cap only bounds a
# pathological log. Pages are small so the paging path is real.
MAX_EVENTS = 200
PAGE_SIZE = 50
FIELDS = ("seq", "type", "at", "status", *publisher.RUN_FIELDS, *publisher.TRIAGE_FIELDS)
_DATA_FIELDS = (*publisher.RUN_FIELDS, *publisher.TRIAGE_FIELDS)
_NAMES = {
    "#pk": "approval_id",
    "#q": "seq",
    "#t": "event_type",
    "#s": "status",
    "#a": "at",
    "#d": "data",
    **{f"#f{i}": name for i, name in enumerate(_DATA_FIELDS)},
}
# Nested paths only: a bare #d beside #d.#f0 would overlap, which DynamoDB
# rejects, and every name above must be used by the request.
_PROJECTION = ", ".join(
    ["#q", "#t", "#s", "#a", *(f"#d.#f{i}" for i in range(len(_DATA_FIELDS)))]
)


def _seq(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        number = int(value)
    elif isinstance(value, int):
        number = value
    else:
        return None
    return number if number >= 1 else None


def public_event(row: dict[str, Any]) -> dict[str, Any] | None:
    """One outbox row as its whitelisted ledger entry; None when the row
    has no usable seq or an unknown event type."""
    seq = _seq(row.get("seq"))
    event_type = row.get("event_type")
    if seq is None or event_type not in publisher.EVENT_NAMES:
        return None
    event: dict[str, Any] = {"seq": seq, "type": event_type, "at": str(row.get("at") or "")}
    status = row.get("status")
    if isinstance(status, str) and status:
        event["status"] = status
    data = row.get("data")
    if isinstance(data, dict):
        for name in _DATA_FIELDS:
            if name not in data:
                continue
            try:
                value = publisher._triage(name, data[name])
            except publisher.Unmappable:
                continue
            if value is not None:
                event[name] = value
    return event


def read(approval_id: str) -> list[dict[str, Any]]:
    """The run's ledger, oldest first, read consistently page by page."""
    rows: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": common.EVENTS_TABLE,
        "KeyConditionExpression": "#pk = :id",
        "ProjectionExpression": _PROJECTION,
        "ExpressionAttributeNames": dict(_NAMES),
        "ExpressionAttributeValues": {":id": approval_id},
        "ConsistentRead": True,
        "ScanIndexForward": True,
        "Limit": PAGE_SIZE,
    }
    while len(rows) < MAX_EVENTS:
        response = common._client.query(**kwargs)
        rows.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    events = (public_event(row) for row in rows[:MAX_EVENTS])
    return [event for event in events if event is not None]

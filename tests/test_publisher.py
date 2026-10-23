"""The outbox relay (approvals/lambda/publisher.py), offline.

The stream records are built from the rows the real Unit of Work puts
on lachesis-run-events (a recorder stands in for DynamoDB), serialized
the way a NEW_IMAGE stream delivers them, so the tests follow the true
outbox shape. EventBridge is an in-memory fake that can refuse entries.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "approvals" / "lambda"
sys.path.insert(0, str(LAMBDA_DIR))

EVENTS_TABLE = "lachesis-run-events"
BASE_FIELDS = {"event_id", "run_ref", "seq", "type", "at", "status"}
TRIAGE = {"tool", "layer", "risk", "confidence", "latency_ms", "cached"}
# The run fields each event of _full_run carries into the detail (B3).
RUN_FIELDS_BY_TYPE = {
    "RunRequested": {"lane"},
    "RunClaimed": set(),
    "RunPaused": {"pause_seq"},
    "DecisionRecorded": {"pause_seq"},
    "RunResuming": {"pause_seq"},
    "RunFailed": {"pause_seq"},
}
SESSION = "s" * 40


def _ref(approval_id):
    """run_ref computed here, not through the publisher: the first 32 hex
    digits of the SHA-256 of the approval_id."""
    return hashlib.sha256(approval_id.encode("utf-8")).hexdigest()[:32]


class Recorder:
    """Accepts every TransactWriteItems and keeps the event puts."""

    def __init__(self):
        self.event_rows = []

    def transact_write_items(self, TransactItems):  # noqa: N803
        for entry in TransactItems:
            put = entry.get("Put")
            if put and put["TableName"] == EVENTS_TABLE:
                self.event_rows.append(put["Item"])
        return {}


class FakeBus:
    """put_events that accepts every entry unless told otherwise."""

    def __init__(self):
        self.calls = []
        self.refuse = set()  # entry indexes within a call to refuse
        self.raise_on_call = set()  # call numbers (0-based) that raise
        self.drop_entries = False

    def put_events(self, Entries):  # noqa: N803
        number = len(self.calls)
        self.calls.append([dict(entry) for entry in Entries])
        if number in self.raise_on_call:
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "PutEvents")
        if self.drop_entries:
            return {"FailedEntryCount": 0, "Entries": []}
        results = []
        for index, _ in enumerate(Entries):
            if index in self.refuse:
                results.append(
                    {"ErrorCode": "InternalFailure", "ErrorMessage": "try again later"}
                )
            else:
                results.append({"EventId": f"eb-{number}-{index}"})
        failed = sum(1 for result in results if "ErrorCode" in result)
        return {"FailedEntryCount": failed, "Entries": results}


@pytest.fixture()
def layers(monkeypatch):
    """Fresh common, approval.* and publisher modules, as the Lambda
    asset loads them."""
    for name in [n for n in sys.modules if n == "approval" or n.startswith("approval.")]:
        monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location("common", LAMBDA_DIR / "common.py")
    common = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "common", common)
    spec.loader.exec_module(common)
    spec = importlib.util.spec_from_file_location("publisher", LAMBDA_DIR / "publisher.py")
    publisher = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "publisher", publisher)
    spec.loader.exec_module(publisher)
    bus = FakeBus()
    monkeypatch.setattr(publisher, "_events_client", bus)
    domain = importlib.import_module("approval.domain")
    uow = importlib.import_module("approval.uow")
    assert uow.common is common
    return domain, uow, publisher, bus


def _full_run(domain, uow, approval_id="appr-pub-1"):
    """A run that is requested, claimed, paused on a destructive write,
    approved, resumed and then fails: every outbox row it wrote."""
    recorder = Recorder()
    work = uow.DynamoUnitOfWork(
        client=recorder, table="lachesis-approvals", events_table=EVENTS_TABLE
    )
    now = time.time()
    run = domain.Run.request(approval_id, SESSION, "approval", now, 1800)
    work.commit(run)
    run.claim("req-secret-77")
    work.commit(run)
    run.pause(
        [{"interruptId": "int-secret-7", "name": "lachesis-confirm", "reason": "SECRET reason"}],
        {
            "tool": "delete_file",
            "risk": "destructive_write",
            "confidence": 0.31,
            "layer": "decider",
            "latency_ms": 187.4,
            "verdict": "confirm",
            "arguments": {"path": "prod_secret.py"},
            "reason": "SECRET reason",
        },
        now,
        1800,
    )
    work.commit(run)
    run.decide("approve", "sub-secret-1", "owner-name", now)
    work.commit(run)
    run.begin_resume(now)
    work.commit(run)
    run.fail("ValueError: secret detail from the agent")
    work.commit(run)
    return recorder.event_rows


def _record(row, sequence, event_name="INSERT"):
    serializer = TypeSerializer()
    image = {key: serializer.serialize(value) for key, value in row.items()}
    return {
        "eventID": f"evt-{sequence}",
        "eventName": event_name,
        "eventSource": "aws:dynamodb",
        "dynamodb": {
            "Keys": {"approval_id": image["approval_id"], "seq": image["seq"]},
            "NewImage": image,
            "SequenceNumber": str(sequence),
            "StreamViewType": "NEW_IMAGE",
        },
    }


def _records(rows, start=100):
    return [_record(row, start + index) for index, row in enumerate(rows)]


def _details(bus):
    return [json.loads(entry["Detail"]) for call in bus.calls for entry in call]


def test_each_event_maps_to_one_whitelisted_entry(layers):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow)
    out = publisher.handler({"Records": _records(rows)}, None)
    assert out == {"batchItemFailures": []}
    [call] = bus.calls
    assert [entry["DetailType"] for entry in call] == [
        "RunRequested",
        "RunClaimed",
        "RunPaused",
        "DecisionRecorded",
        "RunResuming",
        "RunFailed",
    ]
    for entry in call:
        assert set(entry) == {"Source", "DetailType", "Detail", "EventBusName"}
        assert entry["Source"] == "lachesis.approvals"
        assert entry["EventBusName"] == "lachesis-events"
    details = _details(bus)
    for detail in details:
        expected = (
            BASE_FIELDS
            | RUN_FIELDS_BY_TYPE[detail["type"]]
            | (TRIAGE if detail["type"] == "RunPaused" else set())
        )
        assert set(detail) == expected, detail["type"]
        assert set(detail) <= set(publisher.DETAIL_FIELDS)
    assert [d["seq"] for d in details] == [1, 2, 3, 4, 5, 6]
    assert [d["event_id"] for d in details] == [f"{_ref('appr-pub-1')}#{n}" for n in range(1, 7)]
    assert {d["run_ref"] for d in details} == {_ref("appr-pub-1")}
    assert [d["status"] for d in details] == [
        "STARTING",
        "STARTING",
        "PENDING",
        "APPROVED",
        "RESUMING",
        "ERROR",
    ]
    paused = details[2]
    assert paused["tool"] == "delete_file" and paused["risk"] == "destructive_write"
    assert paused["layer"] == "decider"
    # Decimal from the stream back to plain JSON numbers.
    assert paused["confidence"] == 0.31 and isinstance(paused["confidence"], float)
    assert paused["latency_ms"] == 187.4 and isinstance(paused["latency_ms"], float)
    assert all(d["at"] for d in details)


def test_nothing_outside_the_whitelist_reaches_the_bus(layers):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow)
    # The outbox row itself holds more than the bus may see.
    assert any("decided_by_name" in row["data"] for row in rows)
    publisher.handler({"Records": _records(rows)}, None)
    dumped = json.dumps(bus.calls)
    for secret in (
        "appr-pub-1",
        "approval_id",
        "SECRET",
        "prod_secret.py",
        "arguments",
        "int-secret-7",
        "lachesis-confirm",
        "sub-secret-1",
        "owner-name",
        "req-secret-77",
        "secret detail",
        "ValueError",
        SESSION,
        # pause_seq is on the bus since B3 (the audit lake pairs a
        # decision with its pause); expires_ts still is not.
        "expires_ts",
        "run_version",
        "ttl",
        "verdict",
        "from_status",
    ):
        assert secret not in dumped, secret


def test_only_inserts_are_published(layers):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow)
    records = [
        _record(rows[0], 1),
        _record(rows[1], 2, "MODIFY"),
        _record(rows[2], 3),
        _record(rows[3], 4, "REMOVE"),
    ]
    assert publisher.handler({"Records": records}, None) == {"batchItemFailures": []}
    assert [d["seq"] for d in _details(bus)] == [1, 3]


def test_a_batch_without_inserts_makes_no_call(layers):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow)
    records = [_record(rows[0], 1, "REMOVE"), _record(rows[1], 2, "MODIFY")]
    assert publisher.handler({"Records": records}, None) == {"batchItemFailures": []}
    assert publisher.handler({"Records": []}, None) == {"batchItemFailures": []}
    assert bus.calls == []


def test_a_refused_entry_reports_its_own_record(layers):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow)
    records = [
        _record(rows[0], 501),
        _record(rows[1], 502, "MODIFY"),
        _record(rows[2], 503),
        _record(rows[3], 504, "REMOVE"),
        _record(rows[4], 505),
    ]
    # Entry 1 of the call is the third record: skipped records must not
    # shift a failure onto the wrong sequence number.
    bus.refuse = {1}
    out = publisher.handler({"Records": records}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "503"}]}
    assert [d["seq"] for d in _details(bus)] == [1, 3, 5]


def test_put_events_raising_fails_only_its_chunk(layers):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow, "appr-a") + _full_run(domain, uow, "appr-b")
    assert len(rows) == 12
    bus.raise_on_call = {1}
    out = publisher.handler({"Records": _records(rows, start=900)}, None)
    # 12 entries go out as 10 + 2; the second call raised.
    assert [len(call) for call in bus.calls] == [10, 2]
    assert out == {"batchItemFailures": [{"itemIdentifier": "910"}, {"itemIdentifier": "911"}]}


def test_an_entry_without_event_id_counts_as_unsent(layers):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow)[:2]
    bus.drop_entries = True
    out = publisher.handler({"Records": _records(rows, start=10)}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "10"}, {"itemIdentifier": "11"}]}


def test_redelivery_carries_the_same_event_id(layers):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow)
    records = _records(rows)
    publisher.handler({"Records": records}, None)
    publisher.handler({"Records": records}, None)
    first, second = bus.calls
    assert first == second
    # A consumer that dedupes on event_id sees each event exactly once.
    seen = {}
    for detail in _details(bus):
        seen.setdefault(detail["event_id"], detail)
    assert len(seen) == len(rows)
    assert sorted(seen) == sorted(f"{_ref(row['approval_id'])}#{int(row['seq'])}" for row in rows)


def test_an_unmappable_record_is_reported_not_dropped(layers):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow)[:2]
    bogus = dict(rows[1], event_type="NotAnEvent")
    out = publisher.handler({"Records": [_record(rows[0], 1), _record(bogus, 2)]}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "2"}]}
    assert [d["seq"] for d in _details(bus)] == [1]
    mismatched = dict(rows[1], event_id="appr-other#9")
    assert publisher.handler({"Records": [_record(mismatched, 3)]}, None) == {
        "batchItemFailures": [{"itemIdentifier": "3"}]
    }
    no_image = {"eventName": "INSERT", "dynamodb": {"SequenceNumber": "4"}}
    assert publisher.handler({"Records": [no_image]}, None) == {
        "batchItemFailures": [{"itemIdentifier": "4"}]
    }


def test_a_failure_with_no_sequence_number_fails_the_batch(layers):
    _, _, publisher, _ = layers
    with pytest.raises(RuntimeError):
        publisher.handler({"Records": [{"eventName": "INSERT", "dynamodb": {}}]}, None)


def test_a_row_written_before_status_omits_it(layers):
    domain, uow, publisher, bus = layers
    legacy = {key: value for key, value in _full_run(domain, uow)[0].items() if key != "status"}
    publisher.handler({"Records": [_record(legacy, 1)]}, None)
    [detail] = _details(bus)
    assert set(detail) == (BASE_FIELDS - {"status"}) | {"lane"}


def test_stack_names_match_the_publisher(layers):
    """infra/stacks/approvals.py cannot be synthesized here (no CDK); its
    bus name and event source are plain constants, read with ast, and
    must be what the publisher puts and the RunFailed rule matches."""
    import ast

    stack = Path(__file__).resolve().parents[1] / "infra" / "stacks" / "approvals.py"
    constants = {
        node.targets[0].id: node.value.value
        for node in ast.parse(stack.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    _, _, publisher, _ = layers
    assert constants["EVENT_BUS_NAME"] == publisher.BUS_NAME == "lachesis-events"
    assert constants["EVENT_SOURCE"] == publisher.SOURCE == "lachesis.approvals"
    assert "RunFailed" in publisher.EVENT_NAMES
    # The RunFailed alarm watches the embedded metric the publisher prints.
    assert constants["OUTBOX_METRIC_NAMESPACE"] == publisher.METRIC_NAMESPACE
    assert constants["RUN_FAILED_METRIC"] == publisher.RUN_FAILED_METRIC


def _paused_row(domain, uow, **data):
    """The RunPaused outbox row of a full run, with data fields replaced."""
    row = dict(_full_run(domain, uow)[2])
    assert row["event_type"] == "RunPaused"
    row["data"] = dict(row["data"], **data)
    return row


def test_a_non_scalar_triage_value_fails_only_its_record(layers):
    domain, uow, publisher, bus = layers
    good = _full_run(domain, uow)[0]
    bad = _paused_row(domain, uow, tool=["a", Decimal(1)])
    out = publisher.handler({"Records": [_record(good, 1), _record(bad, 2)]}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "2"}]}
    assert [d["seq"] for d in _details(bus)] == [1]


@pytest.mark.parametrize(
    "data",
    [
        {"tool": {"nested": "map"}},
        {"risk": {"a", "b"}},
        {"layer": 7},
        {"confidence": "0.9"},
        {"latency_ms": True},
        {"confidence": Decimal("NaN")},
    ],
)
def test_a_triage_value_of_the_wrong_type_is_unmappable(layers, data):
    domain, uow, publisher, bus = layers
    row = _paused_row(domain, uow, **data)
    if isinstance(next(iter(data.values())), Decimal):
        # A NaN cannot be written through TypeSerializer; put it as the
        # stream would carry it, as a raw N value.
        record = _record(_paused_row(domain, uow), 7)
        name = next(iter(data))
        record["dynamodb"]["NewImage"]["data"]["M"][name] = {"N": "NaN"}
    else:
        record = _record(row, 7)
    assert publisher.handler({"Records": [record]}, None) == {
        "batchItemFailures": [{"itemIdentifier": "7"}]
    }
    assert bus.calls == []


def test_numbers_keep_their_type_whatever_the_value(layers):
    domain, uow, publisher, bus = layers
    row = _paused_row(domain, uow, confidence=Decimal("1.0"), latency_ms=Decimal("200"))
    publisher.handler({"Records": [_record(row, 1)]}, None)
    [call] = bus.calls
    raw = call[0]["Detail"]
    assert '"confidence":1.0' in raw and '"latency_ms":200.0' in raw
    [detail] = _details(bus)
    assert isinstance(detail["confidence"], float) and isinstance(detail["latency_ms"], float)
    assert isinstance(detail["seq"], int)


def test_null_triage_values_stay_null(layers):
    domain, uow, publisher, bus = layers
    row = _paused_row(domain, uow, tool=None, confidence=None)
    publisher.handler({"Records": [_record(row, 1)]}, None)
    [detail] = _details(bus)
    assert detail["tool"] is None and detail["confidence"] is None


def _metric_lines(text):
    return [json.loads(line) for line in text.splitlines() if '"_aws"' in line]


def test_an_accepted_run_failed_writes_one_metric_line(layers, capsys):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow)
    capsys.readouterr()
    publisher.handler({"Records": _records(rows)}, None)
    [line] = _metric_lines(capsys.readouterr().out)
    [directive] = line["_aws"]["CloudWatchMetrics"]
    assert directive["Namespace"] == "Lachesis/Outbox"
    assert directive["Dimensions"] == [["Bus"]]
    assert directive["Metrics"] == [{"Name": "RunFailedPublished", "Unit": "Count"}]
    assert line["Bus"] == "lachesis-events"
    assert line["RunFailedPublished"] == 1
    assert isinstance(line["_aws"]["Timestamp"], int)


def test_a_refused_run_failed_writes_no_metric_line(layers, capsys):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow)
    assert rows[5]["event_type"] == "RunFailed"
    bus.refuse = {5}
    capsys.readouterr()
    out = publisher.handler({"Records": _records(rows)}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "105"}]}
    assert _metric_lines(capsys.readouterr().out) == []


def test_logs_never_carry_the_raw_approval_id(layers, capsys):
    domain, uow, publisher, bus = layers
    rows = _full_run(domain, uow, "appr-token-xyz")
    bus.refuse = {0, 1, 2, 3, 4, 5}
    capsys.readouterr()
    publisher.handler({"Records": _records(rows)}, None)
    logged = capsys.readouterr().out
    assert "publisher entry failed" in logged
    assert _ref("appr-token-xyz") in logged
    assert "appr-token-xyz" not in logged


def test_lane_and_pause_seq_reach_the_detail(layers):
    """B3: the audit lake keeps the approval lane apart from /try and
    pairs a decision with the pause it answered."""
    domain, uow, publisher, bus = layers
    publisher.handler({"Records": _records(_full_run(domain, uow))}, None)
    details = _details(bus)
    assert details[0]["lane"] == "approval"
    assert [d.get("pause_seq") for d in details] == [None, None, 1, 1, 1, 1]
    assert all(isinstance(d["pause_seq"], int) for d in details[2:])
    assert publisher.DETAIL_FIELDS == (
        "event_id", "run_ref", "seq", "type", "at", "status", "lane", "pause_seq",
        "tool", "layer", "risk", "confidence", "latency_ms", "cached",
    )


@pytest.mark.parametrize(
    "data",
    [
        {"pause_seq": Decimal("1.5")},
        {"pause_seq": Decimal("-1")},
        {"pause_seq": "1"},
        {"lane": 3},
        {"cached": Decimal("1")},
        {"cached": "true"},
    ],
)
def test_a_run_field_of_the_wrong_type_is_unmappable(layers, data):
    domain, uow, publisher, bus = layers
    row = _paused_row(domain, uow, **data)
    assert publisher.handler({"Records": [_record(row, 9)]}, None) == {
        "batchItemFailures": [{"itemIdentifier": "9"}]
    }
    assert bus.calls == []


def test_cached_reaches_the_detail_as_a_json_boolean(layers):
    """B3: a RunPaused says whether its verdict was re-issued from the
    handler's cache (no new model call); the stream's BOOL stays a bool."""
    domain, uow, publisher, bus = layers
    fresh = _paused_row(domain, uow)
    reissued = _paused_row(domain, uow, cached=True)
    out = publisher.handler({"Records": [_record(fresh, 1), _record(reissued, 2)]}, None)
    assert out == {"batchItemFailures": []}
    flags = [d["cached"] for d in _details(bus)]
    assert flags == [False, True]
    assert all(isinstance(flag, bool) for flag in flags)
    sent = [entry["Detail"].replace(" ", "") for call in bus.calls for entry in call]
    assert any('"cached":true' in detail for detail in sent)

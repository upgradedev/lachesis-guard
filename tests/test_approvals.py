"""Approvals logic tests (B1 acceptance, offline): a repeat decision is
refused, a non-approver gets 403, a duplicate resume resumes once, and
the /try daily counter stops at the cap. DynamoDB is an in-memory fake
(FakeDynamo) that evaluates the conditions the Unit of Work writes and
applies a TransactWriteItems call all or nothing.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
import time
from dataclasses import fields as dataclass_fields
from decimal import Decimal
from pathlib import Path

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "approvals" / "lambda"
sys.path.insert(0, str(LAMBDA_DIR))


APPROVALS = "lachesis-approvals"
EVENTS = "lachesis-run-events"

_TOKEN = re.compile(r"\s*(attribute_not_exists|attribute_exists|AND|OR|<>|[()=<]|#\w+|:\w+)")
_ASSIGNMENT = re.compile(
    r"(#\w+)\s*=\s*(?:if_not_exists\((#\w+),\s*(:\w+)\)(?:\s*\+\s*(:\w+))?|(:\w+))"
)


def _reject_floats(value):
    """boto3's serializer refuses floats; so does this fake."""
    if isinstance(value, float):
        raise TypeError("Float types are not supported. Use Decimal types instead.")
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_floats(item)


def _as_read(value):
    """DynamoDB returns every number as Decimal."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, dict):
        return {key: _as_read(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_read(item) for item in value]
    return value


def evaluate(expression, item, names, values):
    """The condition grammar the Unit of Work and the counters use:
    attribute_(not_)exists(#n), #n = :v, #n <> :v, #n < :v, AND, OR,
    parentheses."""
    tokens = []
    position = 0
    while position < len(expression):
        match = _TOKEN.match(expression, position)
        if not match:
            if expression[position:].strip() == "":
                break
            raise ValueError(f"unsupported condition: {expression[position:]!r}")
        tokens.append(match.group(1))
        position = match.end()
    item = item or {}

    def operand(token):
        if token.startswith("#"):
            return item.get(names[token])
        return values[token]

    def factor(index):
        token = tokens[index]
        if token == "(":
            result, index = disjunction(index + 1)
            assert tokens[index] == ")"
            return result, index + 1
        if token in {"attribute_not_exists", "attribute_exists"}:
            assert tokens[index + 1] == "(" and tokens[index + 3] == ")"
            present = names[tokens[index + 2]] in item
            return (present if token == "attribute_exists" else not present), index + 4
        left, op, right = operand(token), tokens[index + 1], operand(tokens[index + 2])
        if op == "=":
            result = left is not None and left == right
        elif op == "<>":
            result = left != right
        elif op == "<":
            result = left is not None and left < right
        else:
            raise ValueError(op)
        return result, index + 3

    def conjunction(index):
        result, index = factor(index)
        while index < len(tokens) and tokens[index] == "AND":
            right, index = factor(index + 1)
            result = result and right
        return result, index

    def disjunction(index):
        result, index = conjunction(index)
        while index < len(tokens) and tokens[index] == "OR":
            right, index = conjunction(index + 1)
            result = result or right
        return result, index

    result, index = disjunction(0)
    assert index == len(tokens), expression
    return result


def apply_update(item, expression, names, values):
    assert expression.startswith("SET "), expression
    found = _ASSIGNMENT.findall(expression)
    assert found, expression
    for target, source_name, default, increment, plain in found:
        if plain:
            item[names[target]] = copy.deepcopy(values[plain])
            continue
        current = item.get(names[source_name], values[default])
        item[names[target]] = current + values[increment] if increment else current


class FakeDynamo:
    """Both tables, in memory. TransactWriteItems is all or nothing: every
    condition is evaluated first, and on any failure nothing is applied
    and the error carries CancellationReasons in request order, the way
    DynamoDB reports them."""

    def __init__(self):
        self.tables = {APPROVALS: {}, EVENTS: {}}
        self.transactions = []
        self.inject = []  # reason-code lists to fail the next calls with
        self.queries = []  # every query on the event log (the run ledger)
        self.returned = []  # every item those queries handed back

    @staticmethod
    def _key(table, key):
        return (key["approval_id"], int(key["seq"])) if table == EVENTS else key["approval_id"]

    def get_item(self, TableName, Key, ConsistentRead=False):  # noqa: N803
        item = self.tables[TableName].get(self._key(TableName, Key))
        return {"Item": _as_read(copy.deepcopy(item))} if item else {}

    def transact_write_items(self, TransactItems):  # noqa: N803
        _reject_floats(TransactItems)
        self.transactions.append(copy.deepcopy(TransactItems))
        if self.inject:
            codes = self.inject.pop(0)
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [{"Code": code} for code in codes],
                },
                "TransactWriteItems",
            )
        touched, reasons, plans = set(), [], []
        for entry in TransactItems:
            (op, spec), = entry.items()
            table = spec["TableName"]
            key = self._key(table, spec["Item"] if op == "Put" else spec["Key"])
            if (table, key) in touched:
                raise ClientError(
                    {"Error": {"Code": "ValidationException"}}, "TransactWriteItems"
                )
            touched.add((table, key))
            current = self.tables[table].get(key)
            condition = spec.get("ConditionExpression")
            names = spec.get("ExpressionAttributeNames", {})
            values = spec.get("ExpressionAttributeValues", {})
            ok = condition is None or evaluate(condition, current, names, values)
            reasons.append({"Code": "None" if ok else "ConditionalCheckFailed"})
            plans.append((op, table, key, spec, current))
        if any(reason["Code"] != "None" for reason in reasons):
            raise ClientError(
                {"Error": {"Code": "TransactionCanceledException"}, "CancellationReasons": reasons},
                "TransactWriteItems",
            )
        for op, table, key, spec, current in plans:
            if op == "Put":
                self.tables[table][key] = copy.deepcopy(spec["Item"])
            else:
                item = copy.deepcopy(current) if current else dict(spec["Key"])
                apply_update(
                    item,
                    spec["UpdateExpression"],
                    spec.get("ExpressionAttributeNames", {}),
                    spec.get("ExpressionAttributeValues", {}),
                )
                self.tables[table][key] = item
        return {}

    def query(self, TableName, KeyConditionExpression, **kwargs):  # noqa: N803
        """lachesis-run-events by partition key, the way ledger.read asks:
        seq ascending, at most Limit rows a page, resuming after
        ExclusiveStartKey, a LastEvaluatedKey whenever a page is full,
        and only the projected attributes: a top-level name copies that
        attribute, a nested path (#d.#f0) copies that one key of the map,
        and a bare map beside one of its own paths, or an unused name, is
        rejected, the way DynamoDB rejects them."""
        assert TableName == EVENTS and KeyConditionExpression == "#pk = :id"
        self.queries.append(copy.deepcopy(kwargs))
        names = kwargs["ExpressionAttributeNames"]
        approval_id = kwargs["ExpressionAttributeValues"][":id"]
        rows = self.events_of(approval_id)
        start = kwargs.get("ExclusiveStartKey")
        if start is not None:
            rows = [row for row in rows if int(row["seq"]) > int(start["seq"])]
        limit = kwargs.get("Limit")
        page = rows[:limit] if limit is not None else rows
        parts = [part.strip() for part in kwargs["ProjectionExpression"].split(",")]
        paths = [tuple(names[name] for name in part.split(".")) for part in parts]
        tops = {path[0] for path in paths if len(path) == 1}
        used = {name for part in parts for name in part.split(".")} | {"#pk"}
        if set(names) - used or any(len(path) > 1 and path[0] in tops for path in paths):
            raise ClientError({"Error": {"Code": "ValidationException"}}, "Query")
        items = []
        for row in page:
            item = {}
            for top, *rest in paths:
                if top not in row:
                    continue
                if not rest:
                    item[top] = row[top]
                elif isinstance(row[top], dict) and rest[0] in row[top]:
                    item.setdefault(top, {})[rest[0]] = row[top][rest[0]]
            items.append(item)
        self.returned.extend(copy.deepcopy(items))
        response = {"Items": [_as_read(copy.deepcopy(item)) for item in items]}
        if limit is not None and len(page) == limit:
            response["LastEvaluatedKey"] = {"approval_id": approval_id, "seq": page[-1]["seq"]}
        return response

    def events_of(self, approval_id):
        rows = [row for (aid, _), row in self.tables[EVENTS].items() if aid == approval_id]
        return sorted(rows, key=lambda row: int(row["seq"]))


class FakeTable:
    """The lachesis-approvals view common.get and list_by_status read."""

    def __init__(self, dynamo):
        self.dynamo = dynamo
        self.items = dynamo.tables[APPROVALS]
        self.reads = []
        self.queries = []

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        self.reads.append(ConsistentRead)
        item = self.items.get(Key["approval_id"])
        return {"Item": copy.deepcopy(item)} if item else {}

    def query(self, IndexName, KeyConditionExpression, **kwargs):  # noqa: N803
        """The status-index as DynamoDB pages it: ordered by (created_ts,
        approval_id), ascending unless ScanIndexForward is False, at most
        Limit rows, resuming strictly after ExclusiveStartKey by key
        position (never by list offset, so a row that left the status
        between pages cannot shift the next page), and a LastEvaluatedKey
        whenever a page is full, as DynamoDB may return one then."""
        self.queries.append(copy.deepcopy(kwargs))
        status = kwargs["ExpressionAttributeValues"][":status"]
        forward = kwargs.get("ScanIndexForward", True)

        def position(row):
            return (int(row.get("created_ts", 0)), str(row["approval_id"]))

        rows = sorted(
            (i for i in self.items.values() if i.get("status") == status),
            key=position,
            reverse=not forward,
        )
        start = kwargs.get("ExclusiveStartKey")
        if start is not None:
            assert start["status"] == status
            after = position(start)
            rows = [r for r in rows if (position(r) > after if forward else position(r) < after)]
        limit = kwargs.get("Limit")
        page = rows[:limit] if limit is not None else rows
        response = {"Items": [_as_read(copy.deepcopy(i)) for i in page]}
        if limit is not None and len(page) == limit:
            last = page[-1]
            response["LastEvaluatedKey"] = {
                "approval_id": last["approval_id"],
                "status": last["status"],
                "created_ts": last.get("created_ts", 0),
            }
        return response


@pytest.fixture()
def modules(monkeypatch):
    dynamo = FakeDynamo()
    table = FakeTable(dynamo)
    for name in [n for n in sys.modules if n == "approval" or n.startswith("approval.")]:
        monkeypatch.delitem(sys.modules, name)
    # ledger imports `common` too: drop a copy bound to an earlier instance.
    if "ledger" in sys.modules:
        monkeypatch.delitem(sys.modules, "ledger")
    spec = importlib.util.spec_from_file_location("common", LAMBDA_DIR / "common.py")
    common = importlib.util.module_from_spec(spec)
    # The approval package imports `common`: it must be this instance.
    monkeypatch.setitem(sys.modules, "common", common)
    spec.loader.exec_module(common)
    monkeypatch.setattr(common, "_table", table)
    monkeypatch.setattr(common, "_client", dynamo)
    for name in ("api", "runner"):
        spec = importlib.util.spec_from_file_location(name, LAMBDA_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        assert module.common is common
    assert sys.modules["api"].ledger.common is common
    work = sys.modules["approval.uow"]
    assert work.common is common
    # No real pause between contention retries, and no real Lambda call
    # from the sweeper's resume re-dispatch; tests that check either
    # patch these again with a recorder.
    monkeypatch.setattr(work, "_sleep", lambda seconds: None)
    monkeypatch.setattr(sys.modules["runner"], "_invoke_runner", lambda payload: None)
    return common, sys.modules["api"], sys.modules["runner"], table


def _layer(name):
    return sys.modules[f"approval.{name}"]


def _create_row(common, approval_id, session_id, lane):
    """A STARTING row as it was written before the Unit of Work: no
    version, pause_seq or event_seq (the rows already in the table)."""
    created = int(time.time())
    common._table.items[approval_id] = {
        "approval_id": approval_id,
        "session_id": session_id,
        "status": "STARTING",
        "lane": lane,
        "card": "{}",
        "interrupts": "[]",
        "created_ts": created,
        "expires_ts": created + common.TTL_SECONDS,
        "ttl": created + common.TTL_SECONDS + 86400,
    }


def _transition(common, approval_id, expected, target, **fields):
    """Test setup only: force a row from `expected` to `target` in the
    fake, without the Unit of Work (no version bump, no event)."""
    item = common._table.items[approval_id]
    assert item["status"] == expected, (approval_id, item["status"], expected)
    item.update(copy.deepcopy(fields), status=target)
    return dict(item)


def _seed_starting(approval_id, lane="try"):
    """A STARTING row written by the Unit of Work, as POST /try writes it."""
    domain, work = _layer("domain"), _layer("uow")
    run = domain.Run.request(approval_id, "s" * 40, lane, time.time(), 1800)
    work.DynamoUnitOfWork().commit(run)


def _start(runner, prompt, lane, approval_id, context=None):
    _seed_starting(approval_id, lane)
    return runner.start(prompt, approval_id, context=context)


def _event(route, claims=None, body=None, params=None):
    return {
        "routeKey": route,
        "pathParameters": params or {},
        "body": json.dumps(body or {}),
        "requestContext": {"authorizer": {"jwt": {"claims": claims or {}}}},
    }


APPROVER = {"sub": "sub-1", "username": "owner", "cognito:groups": "approvers"}
PLAIN_USER = {"sub": "sub-2", "username": "guest", "cognito:groups": ""}


def test_transition_table_is_pinned(modules):
    domain = _layer("domain")
    assert {status: sorted(targets) for status, targets in domain.TRANSITIONS.items()} == {
        "STARTING": ["DONE", "ERROR", "PENDING", "STARTING"],
        "PENDING": ["APPROVED", "DECLINED", "EXPIRED", "RESUMING"],
        "APPROVED": ["RESUMING"],
        "DECLINED": ["RESUMING"],
        "RESUMING": ["DONE", "ERROR", "EXPIRED", "PENDING", "REFUSED_DONE"],
        "DONE": [],
        "REFUSED_DONE": [],
        "EXPIRED": [],
        "ERROR": [],
    }
    assert set(domain.TRANSITIONS) == set(domain.STATUSES)
    assert {"DONE", "REFUSED_DONE", "EXPIRED", "ERROR"} == domain.TERMINAL
    run = domain.Run.from_item({"approval_id": "a", "status": "DONE"})
    with pytest.raises(domain.InvalidTransition) as refused:
        run.fail("ValueError: late")
    assert refused.value.current == "DONE"
    assert run.changes == {} and run.events == []


def _decide(api, claims, decision, approval_id):
    event = _event(
        "POST /approvals/{id}/decision", claims, {"decision": decision}, {"id": approval_id}
    )
    return api.handler(event, None)


def test_decision_repeat_is_409_and_non_approver_403(modules, monkeypatch):
    common, api, _, _ = modules
    _create_row(common, "appr-2", "s" * 40, "approval")
    _transition(common, "appr-2", "STARTING", "PENDING", interrupts="[]", card="{}")
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    assert _decide(api, PLAIN_USER, "approve", "appr-2")["statusCode"] == 403
    assert _decide(api, APPROVER, "approve", "appr-2")["statusCode"] == 202
    assert _decide(api, APPROVER, "decline", "appr-2")["statusCode"] == 409


def test_resume_only_once(modules, monkeypatch):
    common, _, runner, _ = modules
    _create_row(common, "appr-3", "s" * 40, "approval")
    interrupts = '[{"interruptId": "i1"}]'
    _transition(common, "appr-3", "STARTING", "PENDING", interrupts=interrupts, card="{}")
    _transition(common, "appr-3", "PENDING", "APPROVED", decided_by="sub-1")
    calls = []
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda session_id, payload, **kw: calls.append(payload)
        or {"status": "completed", "text": "done"},
    )
    assert runner.resume("appr-3")["status"] == "DONE"
    assert runner.resume("appr-3")["status"] == "noop"
    assert len(calls) == 1


def test_resume_error_before_send_returns_row_to_pending(modules, monkeypatch):
    # B0 item 3: only a failure that provably never reached the agent may
    # go back to PENDING. This test used a RuntimeError before B0, which
    # proves nothing about the send; it now uses a pre-send error.
    common, _, runner, _ = modules
    _create_row(common, "appr-4", "s" * 40, "approval")
    interrupts = '[{"interruptId": "i1"}]'
    _transition(common, "appr-4", "STARTING", "PENDING", interrupts=interrupts, card="{}")
    _transition(common, "appr-4", "PENDING", "APPROVED", decided_by="sub-1")

    def _boom(session_id, payload, **kw):
        raise EndpointConnectionError(endpoint_url="https://agentcore.invalid")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    assert runner.resume("appr-4")["status"] == "error-resumable"
    row = common.get("appr-4")
    assert row["status"] == "PENDING"
    assert row["sweep_attempts"] == 1


def test_start_card_carries_decider_fields(modules, monkeypatch):
    common, _, runner, _ = modules
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda session_id, payload, **kw: {
            "status": "interrupt",
            "interrupts": [{"interruptId": "i1", "reason": "card text"}],
            "triage": [
                {
                    "tool": "write_file",
                    "risk": "benign",
                    "confidence": 0.76,
                    "grounded": True,
                    "latency_ms": 712.0,
                    "layer": "decider",
                    "verdict": "proceed",
                },
                {
                    "tool": "delete_file",
                    "risk": "destructive_write",
                    "confidence": 0.31,
                    "latency_ms": 187.4,
                    "layer": "decider",
                    "verdict": "confirm",
                },
            ],
            "changed_files": ["cleanup_notes.txt"],
        },
    )
    out = _start(runner, "prompt", "try", "appr-card")
    assert out["status"] == "interrupt"
    import json

    card = json.loads(common.get("appr-card")["card"])
    assert card["risk"] == "destructive_write"
    assert card["confidence"] == 0.31
    assert card["latency_ms"] == 187.4
    assert card["layer"] == "decider"
    assert len(card["prior_steps"]) == 1
    assert card["prior_steps"][0]["tool"] == "write_file"
    assert card["prior_steps"][0]["verdict"] == "proceed"
    assert card["changed_files"] == ["cleanup_notes.txt"]
    assert card["expires_at"]


def test_resume_repause_card_keeps_decider_fields(modules, monkeypatch):
    common, _, runner, _ = modules
    _create_row(common, "appr-5", "s" * 40, "approval")
    common._table.items["appr-5"]["expires_ts"] = 100
    interrupts = '[{"interruptId": "i1"}]'
    _transition(common, "appr-5", "STARTING", "PENDING", interrupts=interrupts, card="{}")
    _transition(common, 
        "appr-5", "PENDING", "APPROVED", decided_by="sub-1", decided_by_name="owner"
    )
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda session_id, payload, **kw: {
            "status": "interrupt",
            "interrupts": [{"interruptId": "i2", "reason": "second pause"}],
            "triage": [
                {
                    "tool": "run_tests",
                    "risk": "destructive_write",
                    "confidence": 0.24,
                    "latency_ms": 171.0,
                    "layer": "decider",
                    "verdict": "confirm",
                }
            ],
        },
    )
    assert runner.resume("appr-5")["status"] == "interrupt-again"
    import json

    row = common.get("appr-5")
    card = json.loads(row["card"])
    assert card["tool"] == "run_tests"
    assert card["risk"] == "destructive_write"
    assert card["confidence"] == 0.24
    assert card["latency_ms"] == 171.0
    assert card["layer"] == "decider"
    assert card["expires_at"]
    assert int(row["expires_ts"]) > 100
    # B0 item 4: a re-pause keeps who decided, and logs the decision.
    assert row["decided_by"] == "sub-1"
    assert row["decided_by_name"] == "owner"
    assert [d["decision"] for d in row["decisions"]] == ["approve"]


def test_empty_try_token_is_404(modules):
    _, api, _, _ = modules
    out = api.handler(_event("GET /try/{token}", {}, None, {"token": ""}), None)
    assert out["statusCode"] == 404


def test_expired_decision_is_409(modules, monkeypatch):
    common, api, _, _ = modules
    _create_row(common, "appr-old", "s" * 40, "approval")
    _transition(common, "appr-old", "STARTING", "PENDING", interrupts="[]", card="{}")
    common._table.items["appr-old"]["expires_ts"] = 1
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    out = _decide(api, APPROVER, "approve", "appr-old")
    assert out["statusCode"] == 409


def test_approver_queue_hides_try_rows(modules):
    common, api, _, _ = modules
    _create_row(common, "appr-q1", "s" * 40, "approval")
    _transition(common, "appr-q1", "STARTING", "PENDING", interrupts="[]", card="{}")
    _create_row(common, "appr-q2", "s" * 40, "try")
    _transition(common, "appr-q2", "STARTING", "PENDING", interrupts="[]", card="{}")
    body = api.handler(_event("GET /approvals", APPROVER), None)
    import json

    ids = [a["approval_id"] for a in json.loads(body["body"])["approvals"]]
    assert ids == ["appr-q1"]


def test_start_failure_lands_in_error(modules, monkeypatch):
    common, _, runner, _ = modules

    def _boom(session_id, payload, **kw):
        raise RuntimeError("agent down")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    out = _start(runner, "prompt", "try", "appr-err")
    assert out["status"] == "error"
    assert common.get("appr-err")["status"] == "ERROR"


def test_sweeper_closes_stale_starting_and_caps_attempts(modules, monkeypatch):
    common, _, runner, _ = modules
    _create_row(common, "appr-stale", "s" * 40, "try")
    common._table.items["appr-stale"]["created_ts"] = 1
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: {"status": "completed", "text": ""}
    )
    out = runner.sweep()
    assert out["closed_starting"] == 1
    assert common.get("appr-stale")["status"] == "ERROR"

    _create_row(common, "appr-x", "s" * 40, "approval")
    _transition(common, "appr-x", "STARTING", "PENDING", interrupts="[]", card="{}")
    _transition(common, "appr-x", "PENDING", "APPROVED")
    _transition(
        common, "appr-x", "APPROVED", "RESUMING", sweep_attempts=2, resumed_decision="APPROVED"
    )
    run = _layer("uow").DynamoUnitOfWork().get("appr-x")
    assert _layer("service").resume_not_sent(run) == "ERROR"
    assert common.get("appr-x")["status"] == "ERROR"


def test_canned_prompt_opens_with_the_dictated_write(modules):
    _, api, _, _ = modules
    prompt = api.CANNED_PROMPT
    assert prompt.index("cleanup_notes.txt") < prompt.index("delete_file")
    assert "Workspace check started: reviewing refunds_prod_config.py status" in prompt
    assert "sk-" not in prompt


def test_sweeper_expires_try_rows_without_waking_the_agent(modules, monkeypatch):
    common, _, runner, _ = modules
    calls = []
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: calls.append(p) or {"status": "completed"}
    )
    for aid, lane in (("appr-try-old", "try"), ("appr-appr-old", "approval")):
        _create_row(common, aid, "s" * 40, lane)
        _transition(common, aid, "STARTING", "PENDING", interrupts="[]", card="{}")
        common._table.items[aid]["expires_ts"] = 1
    out = runner.sweep()
    assert common.get("appr-try-old")["status"] == "EXPIRED"
    assert out["closed_try"] == 1
    # only the approval-lane row resumed its agent (with the refusal)
    assert len(calls) == 1
    assert common.get("appr-appr-old")["status"] == "EXPIRED"
    assert common.get("appr-appr-old").get("resuming_ts") is not None


def test_send_card_escapes_html_and_skips_when_unset(modules, monkeypatch):
    _, _, runner, _ = modules
    sent = []

    class FakeSes:
        def send_email(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(runner.boto3, "client", lambda service, **kw: FakeSes())
    item = {
        "approval_id": "appr-<script>",
        "card": json.dumps({"reason": "bad <b>tag</b>", "expires_at": "2026-10-17T00:00:00Z"}),
    }
    monkeypatch.setattr(runner, "SES_FROM", "")
    monkeypatch.setattr(runner, "SES_TO", "")
    runner._send_card(item)
    assert sent == []

    monkeypatch.setattr(runner, "SES_FROM", "from@example.com")
    monkeypatch.setattr(runner, "SES_TO", "to@example.com")
    runner._send_card(item)
    assert len(sent) == 1
    html_body = sent[0]["Message"]["Body"]["Html"]["Data"]
    assert "<b>tag</b>" not in html_body
    assert "&lt;b&gt;tag&lt;/b&gt;" in html_body
    assert "&lt;script&gt;" in html_body


CONSOLE_DIR = Path(__file__).resolve().parents[1] / "approvals" / "console"
CONSOLE_FILES = ("index.html", "app.css", "app.js")
# The line budget of the three hand-written console files. 900 until B5;
# raised for the run ledger and the Architecture view by the owner's
# directive of 2026-10-20 ("I want the full platform now"), never past 1100.
CONSOLE_LINE_BUDGET = 1000


def _console(name: str) -> str:
    return (CONSOLE_DIR / name).read_text(encoding="utf-8")


def test_console_split_and_budget():
    # config.js is generated by infra/stacks/approvals.py at deploy time and
    # must never be shadowed by a checked-in file.
    assert not (CONSOLE_DIR / "config.js").exists()
    html = _console("index.html")
    assert '<script src="config.js"></script>' in html
    assert "app.js?v=" in html and "app.css?v=" in html
    assert '<link rel="icon" href="data:,">' in html
    for landmark in ("<header", "<aside", "<main", "<footer", "<dialog", "<form"):
        assert landmark in html
    for view in ("home", "try", "queue", "detail", "policy", "metrics", "architecture"):
        assert f'data-view="{view}"' in html
    for elem in ('class="sidebar"', 'id="cases-table"', 'id="thr-tabs"', 'id="drawer"'):
        assert elem in html
    total = sum(len(_console(name).splitlines()) for name in CONSOLE_FILES)
    # Raised from 900 for B5 (run ledger and Architecture view) under the
    # owner's full-platform directive of 2026-10-20; at most 1100.
    assert total <= CONSOLE_LINE_BUDGET, total



def test_console_copy_rules():
    for name in CONSOLE_FILES:
        text = _console(name)
        for dash in ("\u2013", "\u2014", "\\u2013", "\\u2014", "&mdash;", "&ndash;"):
            assert dash not in text, (name, dash)
        # Owner decision 2026-10-19: the hosted demo emails nobody, and no
        # console text may say or imply that it does.
        assert "email" not in text.lower(), name
        assert "never the gate" not in text.replace("The model is never the gate", ""), name
    html = _console("index.html")
    assert "Every tool call is checked before it runs." in html
    assert (
        "Decider scores it. Benign, grounded and at 0.70 or above, it runs; "
        "under the line it pauses, whatever the class." in html
    )
    assert (
        "A call Decider confidently classes as a destructive write pauses for a person "
        "(rule R3). Any call scored under 0.70 pauses too (rule R1), whatever its class." in html
    )
    assert "below the 0.70 gate" not in html
    assert "approve or decline the paused call right here" not in html


def test_console_app_rules():
    js = _console("app.js")
    # Server data reaches the page through textContent only.
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js
    assert "NEW_PASSWORD_REQUIRED" in js
    assert '"/try/" + encodeURIComponent(id) + "/decision"' in js
    for label in ("Waiting for a person", "Finished without running the paused call",
                  "Expired: nobody decided, treated as declined", "Run failed"):
        assert label in js
    assert "Could not complete:" in js
    for piece in ("Decider did not answer (asleep or busy)",
                  ("The GPU scales to zero when idle. This call paused fail-closed: no score, "
                   "and the public demo cannot decide it. "),
                  "that took about 9 minutes when measured on 2026-10-23, not a promise. ",
                  ('"Start another run after " + hhmm(paused + 600000) + " UTC: a run started '
                   'earlier can pause the same way, and it still uses one of today\'s 5 runs. "'),
                  "If Decider was only busy with another call, a run started once that call ends",
                  " UTC and is then treated as declined; nothing runs."):
        assert piece in js
    assert "15000" in js and "10000" in js
    assert "paste a staging key" not in js


def test_console_reason_parser_matches_confirm_prompt():
    """The console parses the pause reason; pin it to the real format."""
    import re

    from lachesis.handler import _confirm_prompt
    from lachesis.policy import decide

    js = _console("app.js")
    pattern = r"Decider: (\S+) at ([0-9.]+), threshold ([0-9.]+); grounded: (yes|no)"
    assert pattern in js
    assert '"\\narguments:\\n"' in js
    verdict = decide("destructive_write", 0.85, 0.9)
    reason = _confirm_prompt("delete_file", {"path": "refunds_prod_config.py"}, verdict, 0.7)
    match = re.search(pattern, reason)
    assert match and match.group(3) == "0.70" and match.group(4) == "yes"
    assert "\narguments:\n" in reason
    for phrase, message in (
        ("pauses this", verdict.message),
        ("is unsure", decide("benign", 0.5, 0.9).message),
        ("does not know the risk class", decide("odd", 0.9, 0.9).message),
    ):
        assert phrase in message and phrase in js


def _post_try(api, ip):
    event = {"routeKey": "POST /try", "requestContext": {"http": {"sourceIp": ip}}}
    return api.handler(event, None)


CAP_TEXT = (
    "daily demo cap reached for this address or overall; the counters reset at 00:00 UTC"
)


def test_try_counter_cap(modules, monkeypatch):
    common, api, _, _ = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    monkeypatch.setattr(common, "TRY_DAILY_CAP", 2)
    monkeypatch.setattr(common, "TRY_IP_DAILY_CAP", 5)
    assert _post_try(api, "203.0.113.7")["statusCode"] == 202
    assert _post_try(api, "203.0.113.7")["statusCode"] == 202
    refused = _post_try(api, "203.0.113.7")
    assert refused["statusCode"] == 429
    assert json.loads(refused["body"]) == {"error": CAP_TEXT}


def test_try_per_ip_cap(modules, monkeypatch):
    common, api, _, _ = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    monkeypatch.setattr(common, "TRY_DAILY_CAP", 100)
    monkeypatch.setattr(common, "TRY_IP_DAILY_CAP", 1)
    assert _post_try(api, "203.0.113.9")["statusCode"] == 202
    assert _post_try(api, "203.0.113.9")["statusCode"] == 429
    assert _post_try(api, "203.0.113.10")["statusCode"] == 202


def test_malformed_json_body_returns_400(modules):
    _, api, _, _ = modules
    for route, params in (
        ("POST /approvals/start", {}),
        ("POST /approvals/{id}/decision", {"id": "appr-1"}),
        ("POST /try/{token}/decision", {"token": "appr-1"}),
    ):
        for bad_body in ("{not-json", "[]", '"string"', "123"):
            ev = {
                "routeKey": route,
                "pathParameters": params,
                "body": bad_body,
                "requestContext": {"authorizer": {"jwt": {"claims": APPROVER}}},
            }
            assert api.handler(ev, None)["statusCode"] == 400


def test_try_decision_and_cross_lane_isolation(modules, monkeypatch):
    common, api, _, _ = modules
    invoked = []
    monkeypatch.setattr(
        api, "_invoke_runner", lambda payload, asynchronous: invoked.append(payload)
    )

    _create_row(common, "appr-try-warm", "s" * 40, "try")
    _transition(common, 
        "appr-try-warm",
        "STARTING",
        "PENDING",
        interrupts='[{"interruptId": "i1"}]',
        card=json.dumps({"layer": "decider", "risk": "destructive_write", "confidence": 0.31}),
    )
    # Approver endpoints must not leak or mutate /try rows
    get_ev = _event("GET /approvals/{id}", APPROVER, None, {"id": "appr-try-warm"})
    assert api.handler(get_ev, None)["statusCode"] == 404
    assert _decide(api, APPROVER, "approve", "appr-try-warm")["statusCode"] == 404

    # Public /try/{token}/decision works once on a warm /try row and 409s on repeat
    res1 = api.handler(
        _event(
            "POST /try/{token}/decision",
            {},
            {"decision": "approve"},
            {"token": "appr-try-warm"},
        ),
        None,
    )
    assert res1["statusCode"] == 202
    assert len(invoked) == 1
    res2 = api.handler(
        _event(
            "POST /try/{token}/decision",
            {},
            {"decision": "decline"},
            {"token": "appr-try-warm"},
        ),
        None,
    )
    assert res2["statusCode"] == 409

    # Cold GPU (layer == unavailable) on /try cannot be resumed publicly
    _create_row(common, "appr-try-cold", "s" * 40, "try")
    _transition(common, 
        "appr-try-cold",
        "STARTING",
        "PENDING",
        interrupts='[{"interruptId": "i1"}]',
        card=json.dumps({"layer": "unavailable", "confidence": 0.0}),
    )
    cold_res = api.handler(
        _event(
            "POST /try/{token}/decision",
            {},
            {"decision": "approve"},
            {"token": "appr-try-cold"},
        ),
        None,
    )
    assert cold_res["statusCode"] == 409

    # /try/{token}/decision cannot touch an approval-lane row
    _create_row(common, "appr-real", "s" * 40, "approval")
    _transition(common, "appr-real", "STARTING", "PENDING", interrupts="[]", card="{}")
    cross = api.handler(
        _event("POST /try/{token}/decision", {}, {"decision": "approve"}, {"token": "appr-real"}),
        None,
    )
    assert cross["statusCode"] == 404


def test_agent_entrypoint_turn_budget_and_workspace_diff(tmp_path, monkeypatch):
    agent_dir = Path(__file__).resolve().parents[1] / "agent"
    sys.path.insert(0, str(agent_dir))
    spec = importlib.util.spec_from_file_location("agent_entry", agent_dir / "entrypoint.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    monkeypatch.setattr(entry, "WORK_ROOT", tmp_path)

    workdir = entry._workdir("s" * 40)
    baseline = entry._snapshot(workdir)
    assert sorted(baseline) == ["refunds_prod_config.py", "test_workspace.py"]
    assert entry._changed_since(workdir, baseline) == []

    (workdir / "cleanup_notes.txt").write_text("note\n", encoding="utf-8")
    assert entry._changed_since(workdir, baseline) == ["cleanup_notes.txt"]

    budget = entry.TurnBudget(max_turns=2)
    budget._count(None)
    budget._count(None)
    with pytest.raises(RuntimeError, match="hosted budget: more than 2 model turns"):
        budget._count(None)


def test_console_motion_respects_reduced_motion_and_never_animates_numbers():
    css = _console("app.css")
    start = css.index("@media (prefers-reduced-motion: no-preference) {\n")
    depth, end = 0, start
    for i in range(start, len(css)):
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        if depth == 0 and css[i] == "}":
            end = i
            break
    outside = css[:start] + css[end:]
    stray = outside.replace("animation-delay", "")
    assert "animation:" not in stray, "animation outside the motion block"
    js = _console("app.js")
    assert "countUp" not in js and "prefers-reduced-motion" in js


# ---------------------------------------------------------------------------
# Slice B0: correctness on the path judges click. One regression test per
# defect, plus the contract reader on its own.
# ---------------------------------------------------------------------------


def _load_contract():
    import contract  # pure module, on sys.path via LAMBDA_DIR

    return contract


INTERRUPT_PAYLOAD = {
    "status": "interrupt",
    "interrupts": [{"interruptId": "i2", "reason": "second pause"}],
    "triage": [{"tool": "run_tests", "verdict": "confirm", "layer": "decider"}],
}


def test_contract_reader_classifies_every_shape():
    contract = _load_contract()
    done = contract.read_result({"status": "completed", "text": "all good", "triage": "x"})
    assert done.kind == contract.COMPLETED and done.text == "all good" and done.triage == []
    paused = contract.read_result(INTERRUPT_PAYLOAD)
    assert paused.kind == contract.INTERRUPT and paused.interrupts[0]["interruptId"] == "i2"
    # The agent's own error bodies come back with HTTP 200 (entrypoint.py).
    for payload, prefix in (
        ({"status": "error", "error": "session_id must be at least 33 characters"}, "AgentError"),
        ({"status": "error"}, "AgentError"),
        ({"status": "weird"}, "UnknownStatus"),
        ({}, "UnknownStatus"),
        (["not", "a", "dict"], "MalformedResult"),
        (None, "MalformedResult"),
        ({"status": "interrupt", "interrupts": []}, "MalformedResult"),
        ({"status": "interrupt", "interrupts": [{"reason": "no id"}]}, "MalformedResult"),
        ({"status": "interrupt", "interrupts": "i1"}, "MalformedResult"),
    ):
        result = contract.read_result(payload)
        assert result.is_error, payload
        assert result.error.split(":", 1)[0] == prefix, (payload, result.error)
    assert contract.read_result({"status": "completed", "text": 7}).text == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "error", "error": "prompt or interrupt_responses required"},
        {"status": "mystery"},
        {"status": "interrupt", "interrupts": []},
    ],
)
def test_start_records_agent_error_as_error_not_done(modules, monkeypatch, payload):
    # Regression: runner.start used to record anything but "interrupt" as
    # DONE, so an agent error body (HTTP 200) showed as "Finished".
    common, _, runner, _ = modules
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: payload)
    out = _start(runner, "prompt", "try", "appr-agent-err")
    assert out["status"] == "error"
    row = common.get("appr-agent-err")
    assert row["status"] == "ERROR"
    assert row["result_text"]


def _approved_row(common, approval_id, status="APPROVED", lane="approval"):
    _create_row(common, approval_id, "s" * 40, lane)
    _transition(common, 
        approval_id, "STARTING", "PENDING", interrupts='[{"interruptId": "i1"}]', card="{}"
    )
    _transition(common, 
        approval_id,
        "PENDING",
        status,
        decided_by="sub-1",
        decided_by_name="owner",
        decided_at="2026-10-20T10:00:00+00:00",
    )


@pytest.mark.parametrize("decision", ["APPROVED", "DECLINED"])
def test_resume_records_agent_error_as_error(modules, monkeypatch, decision):
    # Regression: runner.resume used to map any non-interrupt answer to
    # DONE or REFUSED_DONE from the decision alone.
    common, _, runner, _ = modules
    _approved_row(common, "appr-r-err", decision)
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: {"status": "error", "error": "agent fell over"}
    )
    assert runner.resume("appr-r-err")["status"] == "ERROR"
    row = common.get("appr-r-err")
    assert row["status"] == "ERROR"
    assert row["result_text"] == "AgentError: agent fell over"


@pytest.mark.parametrize(
    ("decision", "final"), [("APPROVED", "DONE"), ("DECLINED", "REFUSED_DONE")]
)
def test_resume_completed_follows_the_decision(modules, monkeypatch, decision, final):
    common, _, runner, _ = modules
    _approved_row(common, "appr-r-ok", decision)
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: {"status": "completed", "text": "t"}
    )
    assert runner.resume("appr-r-ok") == {"status": final, "text": "t"}
    assert common.get("appr-r-ok")["status"] == final


def _client_error(status: int, code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "InvokeAgentRuntime",
    )


AFTER_SEND_ERRORS = [
    RuntimeError("runtime 500"),
    ReadTimeoutError(endpoint_url="https://agentcore.invalid"),
    ConnectionClosedError(endpoint_url="https://agentcore.invalid"),
    _client_error(424, "RuntimeClientError"),
    _client_error(400, "ValidationException"),
    _client_error(500, "InternalServerException"),
    json.JSONDecodeError("Expecting value", "", 0),
    KeyError("response"),
]


@pytest.mark.parametrize("exc", AFTER_SEND_ERRORS, ids=lambda e: type(e).__name__)
def test_resume_failure_after_send_ends_in_error_never_pending(modules, monkeypatch, exc):
    # Regression: any resume exception used to return the row to PENDING,
    # where a person could decide a call that may already have run.
    common, _, runner, _ = modules
    _approved_row(common, "appr-r-sent")

    def _boom(session_id, payload, **kw):
        raise exc

    monkeypatch.setattr(common, "invoke_agent", _boom)
    out = runner.resume("appr-r-sent")
    assert out == {"status": "ERROR", "error": type(exc).__name__}
    row = common.get("appr-r-sent")
    assert row["status"] == "ERROR"
    assert row["result_text"].startswith(type(exc).__name__ + ":")


def test_failed_before_send_is_exact_class_membership(modules):
    common = modules[0]
    assert common.failed_before_send(EndpointConnectionError(endpoint_url="u"))
    assert common.failed_before_send(ConnectTimeoutError(endpoint_url="u"))
    for exc in AFTER_SEND_ERRORS:
        assert not common.failed_before_send(exc), exc


def test_pre_send_failures_are_capped(modules, monkeypatch):
    common, _, runner, _ = modules
    _approved_row(common, "appr-r-cap")

    def _boom(session_id, payload, **kw):
        raise ConnectTimeoutError(endpoint_url="https://agentcore.invalid")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    for expected in ("PENDING", "PENDING", "ERROR"):
        assert runner.resume("appr-r-cap")["current"] == expected
        if expected == "PENDING":
            _transition(common, "appr-r-cap", "PENDING", "APPROVED")
    assert common.get("appr-r-cap")["status"] == "ERROR"


def test_agent_client_makes_exactly_one_attempt(modules, monkeypatch):
    common = modules[0]
    seen = {}

    class FakeBody:
        def read(self):
            return b'{"status": "completed", "text": "ok"}'

    class FakeAgentCore:
        def invoke_agent_runtime(self, **kwargs):
            seen["kwargs"] = kwargs
            return {"response": FakeBody()}

    def fake_client(service, **kwargs):
        seen["service"], seen["config"] = service, kwargs.get("config")
        return FakeAgentCore()

    monkeypatch.setattr(common.boto3, "client", fake_client)
    assert common.invoke_agent("s" * 40, {"prompt": "p"})["status"] == "completed"
    assert seen["service"] == "bedrock-agentcore"
    assert seen["config"].retries["total_max_attempts"] == 1
    assert json.loads(seen["kwargs"]["payload"]) == {"prompt": "p"}


def test_sweeper_rescue_of_stale_resuming_ends_in_error(modules, monkeypatch):
    # Regression: a RESUMING row older than 20 minutes used to go back to
    # PENDING, although its resume may already have run the paused call.
    common, _, runner, _ = modules
    _approved_row(common, "appr-stuck")
    _transition(common, "appr-stuck", "APPROVED", "RESUMING", resuming_ts=1)
    calls = []
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: calls.append(p) or {})
    out = runner.sweep()
    assert out["rescued"] == 1
    row = common.get("appr-stuck")
    assert row["status"] == "ERROR"
    assert common.public_view(row)["result_text"] == "ResumeOutcomeUnknown"
    assert calls == []


def _expired_pending(common, approval_id):
    _create_row(common, approval_id, "s" * 40, "approval")
    _transition(common, 
        approval_id, "STARTING", "PENDING", interrupts='[{"interruptId": "i1"}]', card="{}"
    )
    common._table.items[approval_id]["expires_ts"] = 1


@pytest.mark.parametrize(
    ("raised", "answer", "status"),
    [
        (ReadTimeoutError(endpoint_url="u"), None, "ERROR"),
        (RuntimeError("boom"), None, "ERROR"),
        (EndpointConnectionError(endpoint_url="u"), None, "PENDING"),
        (None, {"status": "error", "error": "agent fell over"}, "ERROR"),
        (None, {"status": "completed", "text": "refused"}, "EXPIRED"),
    ],
)
def test_sweeper_expiry_applies_the_same_rule(modules, monkeypatch, raised, answer, status):
    common, _, runner, _ = modules
    _expired_pending(common, "appr-exp")

    def _invoke(session_id, payload, **kw):
        if raised is not None:
            raise raised
        return answer

    monkeypatch.setattr(common, "invoke_agent", _invoke)
    runner.sweep()
    assert common.get("appr-exp")["status"] == status


def test_repause_keeps_every_decision(modules, monkeypatch):
    # Regression: a re-pause used to blank decided_by and decided_by_name.
    common, api, runner, _ = modules
    _approved_row(common, "appr-twice")
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: INTERRUPT_PAYLOAD)
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    assert runner.resume("appr-twice")["status"] == "interrupt-again"
    second = {"sub": "sub-9", "username": "second", "cognito:groups": "approvers"}
    assert _decide(api, second, "decline", "appr-twice")["statusCode"] == 202
    assert common.get("appr-twice")["decided_at"]
    assert runner.resume("appr-twice")["status"] == "interrupt-again"
    row = common.get("appr-twice")
    assert row["status"] == "PENDING"
    assert row["decided_by_name"] == "second"
    assert [(d["decision"], d["decided_by_name"]) for d in row["decisions"]] == [
        ("approve", "owner"),
        ("decline", "second"),
    ]
    assert row["decisions"][0]["at"] == "2026-10-20T10:00:00+00:00"
    view = common.public_view(row)
    assert view["decisions"] == [
        {key: entry[key] for key in ("decision", "decided_by_name", "at")}
        for entry in row["decisions"]
    ]
    assert "sub-1" not in json.dumps(view) and "sub-9" not in json.dumps(view)
    # The row keeps "second", but nobody has decided this pause yet.
    assert view["decided_by_name"] == ""


def test_try_decision_records_decided_at(modules, monkeypatch):
    common, api, _, _ = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    _create_row(common, "appr-try-at", "s" * 40, "try")
    _transition(common, 
        "appr-try-at", "STARTING", "PENDING", interrupts="[]", card=json.dumps({"layer": "decider"})
    )
    event = _event(
        "POST /try/{token}/decision", {}, {"decision": "decline"}, {"token": "appr-try-at"}
    )
    assert api.handler(event, None)["statusCode"] == 202
    assert common.get("appr-try-at")["decided_at"]


def test_public_view_is_a_whitelist(modules):
    # Regression: public_view returned the raw ERROR text (exception
    # message included) to the anonymous /try lane.
    common = modules[0]
    secret_text = "ClientError: An error occurred (AccessDenied) arn:aws:iam::123456789012:role/x"
    row = {
        "approval_id": "appr-pv",
        "session_id": "lachesis-session-secret-value-0000000000",
        "status": "ERROR",
        "lane": "try",
        "decided_by": "cognito-sub-secret",
        "decided_by_name": "owner",
        "interrupts": json.dumps([{"interruptId": "i1", "reason": "r", "args": {"k": "v"}}]),
        "card": json.dumps(
            {
                "reason": "pause reason",
                "tool": "delete_file",
                "layer": "refusal",
                "args": {"path": "x"},
                "session_id": "lachesis-session-secret-value-0000000000",
                "prior_steps": [{"tool": "write_file", "verdict": "proceed", "args": {"a": 1}}],
            }
        ),
        "result_text": secret_text,
        "decisions": [{"decision": "approve", "decided_by_name": "owner", "at": "t", "sub": "x"}],
        "created_ts": 5,
    }
    view = common.public_view(row)
    assert set(view) == {
        "approval_id", "status", "lane", "card", "decided_by_name",
        "decisions", "result_text", "created_ts", "project",
    }
    # B7a: the queue groups by project; a run without one is the demo's.
    assert view["project"] == "Demo"
    assert view["result_text"] == "ClientError"
    dumped = json.dumps(view)
    for leaked in ("cognito-sub-secret", "lachesis-session-secret", "123456789012", "AccessDenied"):
        assert leaked not in dumped, leaked
    assert "args" not in view["card"] and "session_id" not in view["card"]
    assert view["card"]["prior_steps"] == [{"tool": "write_file", "verdict": "proceed"}]
    assert view["decisions"] == [{"decision": "approve", "decided_by_name": "owner", "at": "t"}]
    dotted = "botocore.exceptions.ReadTimeoutError"
    for text, label in (
        ("", ""),
        ("no colon here at all", "Error"),
        ("Bad name!: x", "Error"),
        (dotted + ": x", dotted),
    ):
        assert common.public_view({**row, "result_text": text})["result_text"] == label
    done = common.public_view({**row, "status": "DONE", "result_text": "final: answer"})
    assert done["result_text"] == "final: answer"
    assert common.public_view({**row, "card": "not json"})["card"] == {}
    assert common.public_view({**row, "card": "[1]", "decisions": "x"})["card"] == {}


def test_try_get_returns_the_whitelisted_view(modules):
    common, api, _, _ = modules
    _create_row(common, "appr-try-get", "s" * 40, "try")
    _transition(
        common, "appr-try-get", "STARTING", "ERROR", result_text="ValueError: secret detail"
    )
    out = api.handler(_event("GET /try/{token}", {}, None, {"token": "appr-try-get"}), None)
    body = json.loads(out["body"])
    assert body["result_text"] == "ValueError"
    assert "session_id" not in body and "secret detail" not in out["body"]


def test_console_labels_every_layer_the_pattern_emits():
    import re

    root = Path(__file__).resolve().parents[1] / "src" / "lachesis"
    source = "".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    layers = set(re.findall(r'Verdict\((?:[^()]|\([^()]*\))*?"(\w+)"\s*,?\s*\)', source))
    layers.add("decider")
    assert {"refusal", "unavailable", "secretscan", "tier0"} <= layers
    js = _console("app.js")
    line = next(row for row in js.splitlines() if row.startswith("const LAYER = {"))
    keys = set(re.findall(r"(\w+): \"", line))
    assert layers <= keys, layers - keys


def test_console_try_start_race_keeps_polling():
    # Regression: POST /try returns its token before the async runner
    # writes the row, and the first GET 404 ended the page with "No demo
    # run matches this link".
    js = _console("app.js")
    start = js.index('$("#try-start").addEventListener("click"')
    branch = js[start:js.index("\n});", start)]
    store_at = branch.index('s.setItem("lachesis_try_started"')
    assert store_at < branch.index("location.hash =")
    assert "t: Date.now()" in branch
    view = js[js.index("function viewTry(token)"):start]
    assert 'getItem("lachesis_try_started")' in view
    assert "!denied && mine.token === token && Date.now() - mine.t < 20000" in view
    assert 'renderTry({ status: "STARTING", card: {} }, ctl); ctl.kick(); return;' in view
    assert view.index("ctl.kick()") < view.index("No demo run matches this link")
    # kick() re-arms a stopped poll on a 2 s tick, and 401/403 pass the
    # response, so a denied request is never retried.
    assert "handle = later(tick, 2000)" in js
    assert "on404(out); return;" in js and "on404(); return;" in js
    html = _console("index.html")
    assert "app.js?v=20261023a" in html


def test_the_console_decides_a_gate_run_without_promising_an_agent():
    js = _console("app.js")
    html = _console("index.html")
    assert "app.js?v=20261023a" in html
    # The detail view hands the lane on; the decision path for any lane
    # but try is the approver route.
    assert 'decisionCard(v, gate ? "gate" : "approval", () => ctl.kick())' in js
    assert '"/approvals/" + encodeURIComponent(id) + "/decision"' in js
    assert ('lane === "gate" ? "Recorded. A pull request check still waiting reads it; '
            'otherwise re-run the lachesis-gate job."') in js
    # No agent wording on a gate run: an expired one says the check fails,
    # and its ledger pills read the gate's status words.
    assert ('v.lane === "gate" ? h("li", { class: "stopped" }, h("div", { class: "step-h", '
            'text: "Merge check fails" })') in js
    assert 'lane === "gate" ? "Approve the merge" : "Approve and run"' in js
    assert 'GATE_DONE: ["Merge gate passed", "success"]' in js
    assert 'mergegate: "Merge gate (deterministic scan)"' in js
    # A decided gate run is final: the poll stops.
    assert '(v.lane === "gate" && v.status !== "PENDING")) return;' in js
    # The layer the gate writes is labelled, and the card's layer is it.
    from gate import policy

    assert f'{policy.LAYER}: "' in js


def test_console_hides_previous_approver_on_a_fresh_pause():
    js = _console("app.js")
    assert '["Decided by", view.decided_by_name && view.status !== "PENDING" ?' in js


def _decisions(row):
    return [(d["decision"], d["decided_by_name"]) for d in row.get("decisions", [])]


@pytest.mark.parametrize(
    ("lane", "answer", "final"),
    [
        ("try", None, "EXPIRED"),
        ("approval", {"status": "completed", "text": "refused"}, "EXPIRED"),
        ("approval", {"status": "error", "error": "agent fell over"}, "ERROR"),
    ],
)
def test_expiry_after_a_repause_credits_nobody(modules, monkeypatch, lane, answer, final):
    # Regression: a re-pause keeps the earlier decider in the row, and an
    # expiry of the new pause showed that person as having decided it
    # ("Nobody decided before the deadline. Decided on this page.").
    common, api, runner, _ = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    aid = "appr-exp-" + lane + "-" + final
    _create_row(common, aid, "s" * 40, lane)
    _transition(common, 
        aid,
        "STARTING",
        "PENDING",
        interrupts='[{"interruptId": "i1"}]',
        card=json.dumps({"layer": "decider", "tool": "delete_file"}),
    )
    if lane == "try":
        event = _event("POST /try/{token}/decision", {}, {"decision": "approve"}, {"token": aid})
        assert api.handler(event, None)["statusCode"] == 202
        first = "try-visitor"
    else:
        assert _decide(api, APPROVER, "approve", aid)["statusCode"] == 202
        first = "owner"
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: INTERRUPT_PAYLOAD)
    assert runner.resume(aid)["status"] == "interrupt-again"
    assert common.public_view(common.get(aid))["decided_by_name"] == ""
    common._table.items[aid]["expires_ts"] = 1
    calls = []
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: calls.append(p) or answer)
    runner.sweep()
    row = common.get(aid)
    assert row["status"] == final
    assert len(calls) == (0 if lane == "try" else 1)
    assert row["decided_by"] == "" and row["decided_by_name"] == "" and row["decided_at"] == ""
    assert common.public_view(row)["decided_by_name"] == ""
    assert _decisions(row) == [("approve", first)]
    assert common.public_view(row)["card"]["tool"] == "run_tests"


def test_public_view_names_the_decider_only_once_decided(modules):
    common = modules[0]
    row = {"approval_id": "a", "status": "PENDING", "decided_by_name": "owner"}
    for status, shown in (
        ("PENDING", ""),
        ("EXPIRED", ""),
        ("APPROVED", "owner"),
        ("RESUMING", "owner"),
        ("DONE", "owner"),
        ("ERROR", "owner"),
    ):
        assert common.public_view({**row, "status": status})["decided_by_name"] == shown


def test_pre_send_failure_keeps_the_unsent_decision(modules, monkeypatch):
    # Regression: a resume that failed before send returned the row to
    # PENDING with the decider still set and no history entry, so the
    # next decision overwrote the first and it appeared nowhere.
    common, api, runner, _ = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    _approved_row(common, "appr-unsent")

    def _boom(session_id, payload, **kw):
        raise ConnectTimeoutError(endpoint_url="https://agentcore.invalid")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    assert runner.resume("appr-unsent")["current"] == "PENDING"
    row = common.get("appr-unsent")
    assert row["decided_by"] == "" and row["decided_by_name"] == "" and row["decided_at"] == ""
    assert row["decisions"] == [
        {
            "decision": "approve_not_sent",
            "decided_by_name": "owner",
            "at": "2026-10-20T10:00:00+00:00",
        }
    ]
    second = {"sub": "sub-9", "username": "second", "cognito:groups": "approvers"}
    assert _decide(api, second, "decline", "appr-unsent")["statusCode"] == 202
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: {"status": "completed", "text": "t"}
    )
    assert runner.resume("appr-unsent")["status"] == "REFUSED_DONE"
    view = common.public_view(common.get("appr-unsent"))
    assert view["decided_by_name"] == "second"
    assert _decisions(view) == [("approve_not_sent", "owner")]


def test_sweeper_pre_send_failure_adds_no_person_decision(modules, monkeypatch):
    common, _, runner, _ = modules
    _expired_pending(common, "appr-exp-unsent")

    def _boom(session_id, payload, **kw):
        raise EndpointConnectionError(endpoint_url="u")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    runner.sweep()
    row = common.get("appr-exp-unsent")
    assert row["status"] == "PENDING" and row["sweep_attempts"] == 1
    assert "decisions" not in row


def _try_card(reason, tool):
    return json.dumps({"reason": reason, "tool": tool, "layer": "decider"})


def _split_reason(reason):
    """The console's parseReason split, in Python."""
    rest = reason[reason.index("\narguments:\n") + 12 :]
    cut = rest.rindex("\n\n")
    return json.loads(rest[:cut]), rest[cut + 2 :]


def test_try_lane_reason_keeps_only_whitelisted_arguments(modules):
    import re

    from lachesis.handler import _confirm_prompt
    from lachesis.policy import decide

    common = modules[0]
    verdict = decide("destructive_write", 0.85, 0.9)

    def view(tool, args, lane="try"):
        reason = _confirm_prompt(tool, args, verdict, 0.7)
        row = {
            "approval_id": "a",
            "status": "PENDING",
            "lane": lane,
            "card": _try_card(reason, tool),
        }
        return reason, common.public_view(row)["card"]["reason"]

    long_text = "Workspace check started. " * 40
    original, shown = view(
        "write_file",
        {
            "path": "cleanup_notes.txt",
            "content": long_text,
            "mode": "extra-arg-value",
            "nested": {"k": "v"},
        },
    )
    args, message = _split_reason(shown)
    assert args == {"path": "cleanup_notes.txt", "content": long_text[:200] + "..."}
    assert "extra-arg-value" not in shown and "nested" not in shown
    assert message == verdict.message
    assert shown.split("\narguments:\n")[0] == original.split("\narguments:\n")[0]
    pattern = r"Decider: (\S+) at ([0-9.]+), threshold ([0-9.]+); grounded: (yes|no)"
    assert re.search(pattern, shown)
    _, shown = view("delete_file", {"path": "refunds_prod_config.py", "force": "yes-extra"})
    assert _split_reason(shown)[0] == {"path": "refunds_prod_config.py"}
    _, shown = view("run_tests", {"selector": "extra"})
    assert _split_reason(shown)[0] == {}
    _, shown = view("exfiltrate", {"path": "p", "url": "https://example.invalid"})
    assert _split_reason(shown)[0] == {} and "example.invalid" not in shown
    # The approver lane keeps the full Confirm prompt.
    original, shown = view("delete_file", {"path": "p", "force": "x"}, lane="approval")
    assert shown == original
    # Arguments that are not a JSON object never pass through.
    capped = 't\ntool: read_file\narguments:\n{"path": "x"... [9 more]\n\nm'
    row = {
        "approval_id": "a",
        "status": "PENDING",
        "lane": "try",
        "card": _try_card(capped, "read_file"),
    }
    assert common.public_view(row)["card"]["reason"].endswith("arguments:\n{}\n\nm")
    plain = {**row, "card": _try_card("plain reason", "write_file")}
    assert common.public_view(plain)["card"]["reason"] == "plain reason"


def test_try_counter_rows_carry_a_ttl(modules, monkeypatch):
    _, api, _, table = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    assert _post_try(api, "203.0.113.11")["statusCode"] == 202
    first = {key: row["ttl"] for key, row in table.items.items() if key.startswith("try-")}
    assert len(first) == 2
    for value in first.values():
        assert 0 < value - time.time() <= _layer("service").COUNTER_TTL_SECONDS
    assert _post_try(api, "203.0.113.11")["statusCode"] == 202
    assert {key: table.items[key]["ttl"] for key in first} == first


class _Context:
    def __init__(self, remaining_ms):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self.remaining_ms


def test_agent_timeouts_are_explicit_and_follow_the_lambda_budget(modules, monkeypatch):
    common, _, runner, _ = modules
    config = common.AGENT_CLIENT_CONFIG
    assert config.read_timeout == common.AGENT_READ_TIMEOUT == 840
    assert config.connect_timeout == common.AGENT_CONNECT_TIMEOUT == 10
    assert common.agent_client_config(100).read_timeout == 100
    assert common.agent_client_config(100).retries["total_max_attempts"] == 1
    assert runner._read_timeout(None) == 840
    assert runner._read_timeout(_Context(300_000)) == 280
    assert runner._read_timeout(_Context(2_000_000)) == 840
    assert runner._read_timeout(_Context(10_000)) == 5
    seen = []
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda s, p, **kw: seen.append(kw["read_timeout"]) or {"status": "completed", "text": ""},
    )
    _approved_row(common, "appr-budget")
    runner.handler({"action": "resume", "approval_id": "appr-budget"}, _Context(120_000))
    _seed_starting("appr-budget-start")
    runner.handler(
        {"action": "start", "prompt": "p", "approval_id": "appr-budget-start"},
        _Context(900_000),
    )
    assert seen == [100, 840]


def test_agent_client_uses_the_given_read_timeout(modules, monkeypatch):
    common = modules[0]
    seen = {}

    class FakeBody:
        def read(self):
            return b'{"status": "completed", "text": "ok"}'

    class FakeAgentCore:
        def invoke_agent_runtime(self, **kwargs):
            return {"response": FakeBody()}

    def fake_client(service, **kwargs):
        seen["config"] = kwargs.get("config")
        return FakeAgentCore()

    monkeypatch.setattr(common.boto3, "client", fake_client)
    common.invoke_agent("s" * 40, {"prompt": "p"}, read_timeout=42)
    assert seen["config"].read_timeout == 42
    assert seen["config"].retries["total_max_attempts"] == 1


def test_sweeper_defers_expiry_resumes_without_lambda_time(modules, monkeypatch):
    common, _, runner, _ = modules
    calls = []
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: calls.append(p) or {})
    for aid, lane in (("appr-late-try", "try"), ("appr-late-appr", "approval")):
        _create_row(common, aid, "s" * 40, lane)
        _transition(common, aid, "STARTING", "PENDING", interrupts="[]", card="{}")
        common._table.items[aid]["expires_ts"] = 1
    spec = importlib.util.spec_from_file_location("sweeper", LAMBDA_DIR / "sweeper.py")
    sweeper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweeper)
    monkeypatch.setattr(sweeper, "runner", runner)
    out = sweeper.handler({}, _Context(30_000))
    assert out["deferred"] == 1 and out["closed_try"] == 1
    assert common.get("appr-late-appr")["status"] == "PENDING"
    assert common.get("appr-late-try")["status"] == "EXPIRED"
    assert calls == []


@pytest.mark.parametrize(
    ("errors", "final", "calls_made", "sleeps"),
    [
        ([_client_error(429, "ThrottlingException")] * 2, "completed", 3, [1.0, 2.0]),
        ([_client_error(402, "ServiceQuotaExceededException")], "completed", 2, [1.0]),
        ([ConnectTimeoutError(endpoint_url="u")], "completed", 2, [1.0]),
        ([_client_error(429, "ThrottlingException")] * 3, "error", 3, [1.0, 2.0]),
        ([ReadTimeoutError(endpoint_url="u")], "error", 1, []),
        ([_client_error(500, "InternalServerException")], "error", 1, []),
    ],
    ids=[
        "throttle-twice",
        "quota-once",
        "pre-send-once",
        "throttle-thrice",
        "read-timeout",
        "server-error",
    ],
)
def test_start_retries_only_refusals_that_never_ran(
    modules, monkeypatch, errors, final, calls_made, sleeps
):
    common, _, runner, _ = modules
    pending = list(errors)
    calls, slept = [], []

    def _invoke(session_id, payload, **kw):
        calls.append(payload)
        if pending:
            raise pending.pop(0)
        return {"status": "completed", "text": "ok"}

    monkeypatch.setattr(common, "invoke_agent", _invoke)
    monkeypatch.setattr(runner, "_sleep", slept.append)
    out = _start(runner, "p", "try", "appr-start-retry")
    assert out["status"] == final
    assert len(calls) == calls_made and slept == sleeps
    expected_row = "DONE" if final == "completed" else "ERROR"
    assert common.get("appr-start-retry")["status"] == expected_row


# --- B1: the Unit of Work -------------------------------------------------


class _Lambda:
    def __init__(self, request_id, remaining_ms=900_000):
        self.aws_request_id = request_id
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self.remaining_ms


PAUSE_PAYLOAD = {
    "status": "interrupt",
    "interrupts": [
        {
            "interruptId": "int-secret-7",
            "name": "lachesis-confirm",
            "reason": "tool: delete_file\narguments:\n{\"path\": \"prod_secret.py\"}\n\nok?",
        }
    ],
    "triage": [
        {
            "tool": "delete_file",
            "risk": "destructive_write",
            "confidence": 0.31,
            "latency_ms": 187.4,
            "layer": "decider",
            "verdict": "confirm",
        }
    ],
}


def _events(table, approval_id):
    return table.dynamo.events_of(approval_id)


def test_post_try_commits_counters_row_and_event_in_one_transaction(modules, monkeypatch):
    common, api, _, table = modules
    seen = []

    def _hand_off(payload, asynchronous):
        # The row and its first event exist before the runner is invoked:
        # a poll of the token can never 404 (the race B1 removes).
        row = common.get(payload["approval_id"])
        seen.append((payload, row["status"], len(_events(table, payload["approval_id"]))))
        return {}

    monkeypatch.setattr(api, "_invoke_runner", _hand_off)
    out = _post_try(api, "198.51.100.4")
    assert out["statusCode"] == 202
    token = json.loads(out["body"])["token"]
    assert json.loads(out["body"]) == {"token": token, "status": "STARTING"}
    assert len(table.dynamo.transactions) == 1
    ops = [next(iter(entry)) for entry in table.dynamo.transactions[0]]
    assert ops == ["Update", "Update", "Put", "Put"]
    assert seen == [
        ({"action": "start", "prompt": api.CANNED_PROMPT, "approval_id": token}, "STARTING", 1)
    ]
    row = table.items[token]
    assert row["version"] == 1 and row["event_seq"] == 1 and row["pause_seq"] == 0
    assert row["lane"] == "try" and len(row["session_id"]) >= 33
    # The poll that follows the 202 reads the row strongly consistently.
    table.reads.clear()
    polled = api.handler(_event("GET /try/{token}", {}, None, {"token": token}), None)
    assert polled["statusCode"] == 200 and table.reads == [True]
    [event] = _events(table, token)
    assert event["event_type"] == "RunRequested" and event["seq"] == 1
    assert event["event_id"] == token + "#1" and event["data"] == {"lane": "try"}
    assert "198.51.100.4" not in json.dumps(event, default=str)


def test_a_capped_start_consumes_nothing(modules, monkeypatch):
    common, api, _, table = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    monkeypatch.setattr(common, "TRY_DAILY_CAP", 1)
    monkeypatch.setattr(common, "TRY_IP_DAILY_CAP", 5)
    assert _post_try(api, "198.51.100.1")["statusCode"] == 202
    before = copy.deepcopy(table.dynamo.tables)
    refused = _post_try(api, "198.51.100.2")
    assert refused["statusCode"] == 429
    assert json.loads(refused["body"]) == {"error": CAP_TEXT}
    # The global cap refused the transaction, so the second address's
    # counter, the row and the event were not written either.
    assert table.dynamo.tables == before
    assert not [key for key in table.items if key.startswith("try-ip-198.51.100.2")]


def test_duplicate_start_delivery_is_a_noop(modules, monkeypatch):
    common, _, runner, table = modules
    calls = []
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda s, p, **kw: calls.append(p) or {"status": "completed", "text": "ok"},
    )
    _seed_starting("appr-dup")
    event = {"action": "start", "prompt": "p", "approval_id": "appr-dup"}
    assert runner.handler(event, _Lambda("req-1"))["status"] == "completed"
    # Lambda may deliver one async event twice; neither the same request
    # id nor another one may run the agent again.
    assert runner.handler(event, _Lambda("req-1")) == {"status": "noop", "approval_id": "appr-dup"}
    assert runner.handler(event, _Lambda("req-2")) == {"status": "noop", "approval_id": "appr-dup"}
    assert len(calls) == 1
    assert table.items["appr-dup"]["claimed_request_id"] == "req-1"
    assert [e["event_type"] for e in _events(table, "appr-dup")] == [
        "RunRequested",
        "RunClaimed",
        "RunCompleted",
    ]


def test_concurrent_claims_let_exactly_one_runner_through(modules):
    _, _, _, table = modules
    work, domain, service = _layer("uow").DynamoUnitOfWork(), _layer("domain"), _layer("service")
    _seed_starting("appr-race")
    first, second = work.get("appr-race"), work.get("appr-race")
    first.claim("req-a")
    second.claim("req-b")
    work.commit(first)
    with pytest.raises(domain.Conflict):
        work.commit(second)
    assert table.items["appr-race"]["claimed_request_id"] == "req-a"
    assert service.claim_run("appr-race", "req-c") is None
    assert service.claim_run("appr-missing", "req-d") is None
    # The claim condition names the attribute, besides status and version.
    update = table.dynamo.transactions[1][0]["Update"]
    names = update["ExpressionAttributeNames"]
    assert "claimed_request_id" in names.values()
    assert "attribute_not_exists" in update["ConditionExpression"]


def test_a_start_without_a_written_row_never_invokes_the_agent(modules, monkeypatch):
    common, _, runner, table = modules
    calls = []
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: calls.append(p) or {})
    assert runner.handler({"action": "start", "prompt": "p"}, None)["status"] == "noop"
    assert runner.start("p", "appr-never-written")["status"] == "noop"
    assert calls == [] and "appr-never-written" not in table.items


def test_a_full_run_appends_contiguous_events(modules, monkeypatch):
    common, api, runner, table = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    token = json.loads(_post_try(api, "198.51.100.8")["body"])["token"]
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: PAUSE_PAYLOAD)
    assert runner.start("p", token, context=_Lambda("req-9"))["status"] == "interrupt"
    decide = _event("POST /try/{token}/decision", {}, {"decision": "approve"}, {"token": token})
    assert api.handler(decide, None)["statusCode"] == 202
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: PAUSE_PAYLOAD)
    assert runner.resume(token)["status"] == "interrupt-again"
    assert api.handler(
        _event("POST /try/{token}/decision", {}, {"decision": "decline"}, {"token": token}), None
    )["statusCode"] == 202
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: {"status": "completed", "text": "stood down"}
    )
    assert runner.resume(token) == {"status": "REFUSED_DONE", "text": "stood down"}

    events = _events(table, token)
    assert [e["event_type"] for e in events] == [
        "RunRequested",
        "RunClaimed",
        "RunPaused",
        "DecisionRecorded",
        "RunResuming",
        "RunPaused",
        "DecisionRecorded",
        "RunResuming",
        "RunRefused",
    ]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert [e["event_id"] for e in events] == [f"{token}#{n}" for n in range(1, len(events) + 1)]
    assert [e["run_version"] for e in events] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    # Each event carries the status its command left the run in.
    assert [e["status"] for e in events] == [
        "STARTING",
        "STARTING",
        "PENDING",
        "APPROVED",
        "RESUMING",
        "PENDING",
        "DECLINED",
        "RESUMING",
        "REFUSED_DONE",
    ]
    row = table.items[token]
    assert row["version"] == 9 and row["event_seq"] == 9 and row["pause_seq"] == 2
    assert [e["data"]["pause_seq"] for e in events[2:]] == [1, 1, 1, 2, 2, 2, 2]
    assert events[3]["data"] == {
        "pause_seq": 1,
        "decision": "approve",
        "decided_by_name": "try-visitor",
    }
    assert events[4]["data"] == {"pause_seq": 1, "answer": "approve", "cause": "decision"}
    # Each command was exactly one transaction.
    assert len(table.dynamo.transactions) == 9


def test_events_carry_only_whitelisted_fields(modules, monkeypatch):
    common, api, runner, table = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    _seed_starting("appr-white", "approval")
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: PAUSE_PAYLOAD)
    runner.start("SECRET-PROMPT-TEXT", "appr-white", context=_Lambda("req-w"))
    assert _decide(api, APPROVER, "approve", "appr-white")["statusCode"] == 202

    def _boom(session_id, payload, **kw):
        raise ValueError("secret detail from the agent")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    assert runner.resume("appr-white")["status"] == "ERROR"
    events = _events(table, "appr-white")
    dumped = json.dumps(events, default=str)
    for secret in (
        "SECRET-PROMPT-TEXT",
        "prod_secret.py",
        "arguments",
        "int-secret-7",
        "lachesis-confirm",
        "sub-1",
        "secret detail",
        "s" * 40,
    ):
        assert secret not in dumped, secret
    domain = _layer("domain")
    allowed = {cls.__name__: {f.name for f in dataclass_fields(cls)} for cls in domain.EVENT_TYPES}
    for event in events:
        assert set(event["data"]) == allowed[event["event_type"]]
    paused = next(e for e in events if e["event_type"] == "RunPaused")["data"]
    assert paused["tool"] == "delete_file" and paused["risk"] == "destructive_write"
    # Floats reach DynamoDB as Decimal (boto3 refuses floats).
    assert paused["confidence"] == Decimal("0.31") and paused["latency_ms"] == Decimal("187.4")
    failed = events[-1]
    assert failed["event_type"] == "RunFailed"
    assert failed["data"] == {"pause_seq": 1, "from_status": "RESUMING", "error_type": "ValueError"}
    # The row still keeps the full text for the owner; public_view cuts it.
    assert table.items["appr-white"]["result_text"].startswith("ValueError: secret detail")


def test_non_finite_numbers_never_reach_an_event(modules):
    domain = _layer("domain")
    run = domain.Run.request("appr-nan", "s" * 40, "try", time.time(), 1800)
    _layer("uow").DynamoUnitOfWork().commit(run)
    run.pause(
        [{"interruptId": "i1"}],
        {"tool": "delete_file", "confidence": float("nan"), "latency_ms": float("inf")},
        time.time(),
        1800,
    )
    _layer("uow").DynamoUnitOfWork().commit(run)
    _, _, _, table = modules
    paused = _events(table, "appr-nan")[-1]["data"]
    assert paused["confidence"] is None and paused["latency_ms"] is None


def test_a_stale_version_is_a_conflict_and_writes_nothing(modules, monkeypatch):
    common, api, _, table = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    _create_row(common, "appr-stale-v", "s" * 40, "approval")
    _transition(common, "appr-stale-v", "STARTING", "PENDING", interrupts="[]", card="{}")
    work, domain = _layer("uow").DynamoUnitOfWork(), _layer("domain")
    stale = work.get("appr-stale-v")
    assert stale.version == 0
    # A row written before the Unit of Work: version 0 is conditioned on
    # the attribute being absent.
    assert _decide(api, APPROVER, "approve", "appr-stale-v")["statusCode"] == 202
    first = table.dynamo.transactions[-1][0]["Update"]
    assert "attribute_not_exists" in first["ConditionExpression"]
    assert table.items["appr-stale-v"]["version"] == 1
    _transition(common, "appr-stale-v", "APPROVED", "PENDING")  # same status, newer version
    before = copy.deepcopy(table.dynamo.tables)
    stale.decide("decline", "sub-2", "guest", time.time())
    with pytest.raises(domain.Conflict):
        work.commit(stale)
    assert table.dynamo.tables == before


def test_cancellation_reasons_map_to_domain_errors(modules, monkeypatch):
    _, _, _, table = modules
    uow, domain = _layer("uow"), _layer("domain")

    def cancelled(*codes):
        return ClientError(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [{"Code": code} for code in codes],
            },
            "TransactWriteItems",
        )

    tags = ["cap", "cap", "conflict", "conflict"]
    assert uow.classify(cancelled("None", "ConditionalCheckFailed", "None", "None"), tags) == "cap"
    assert uow.classify(cancelled("None", "None", "ConditionalCheckFailed", "None"), tags) == (
        "conflict"
    )
    assert uow.classify(cancelled("None", "None", "None", "ConditionalCheckFailed"), tags) == (
        "conflict"
    )
    assert uow.classify(cancelled("TransactionConflict", "None", "None", "None"), tags) == "retry"
    assert uow.classify(cancelled("ValidationError"), tags) == "other"
    other = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "T")
    assert uow.classify(other, tags) == "other"

    work = uow.DynamoUnitOfWork()
    pauses = []
    monkeypatch.setattr(uow, "_sleep", pauses.append)
    # A concurrent transaction alone is retried, after a short pause ...
    _seed_starting("appr-tc")
    run = work.get("appr-tc")
    run.claim("req-1")
    table.dynamo.inject = [["TransactionConflict", "None"]] * uow.CONTENTION_RETRIES
    work.commit(run)
    assert table.items["appr-tc"]["claimed_request_id"] == "req-1"
    assert len(pauses) == uow.CONTENTION_RETRIES
    assert all(0.025 <= pause <= 0.150 for pause in pauses)
    # ... and once the retries run out it is Busy, a Conflict.
    run.fail("StartTimeout: test")
    table.dynamo.inject = [["TransactionConflict", "None"]] * (uow.CONTENTION_RETRIES + 1)
    with pytest.raises(domain.Busy) as busy:
        work.commit(run)
    assert isinstance(busy.value, domain.Conflict) and busy.value.http_status == 503
    assert not table.dynamo.inject
    # Anything else is not swallowed.
    run = work.get("appr-tc")
    run.fail("StartTimeout: test")
    table.dynamo.inject = [["ValidationError", "None"]]
    with pytest.raises(ClientError):
        work.commit(run)
    assert table.items["appr-tc"]["status"] == "STARTING"


def test_runner_hand_off_failure_ends_the_run_at_once(modules, monkeypatch):
    _, api, _, table = modules

    def _down(payload, asynchronous):
        raise EndpointConnectionError(endpoint_url="https://lambda.invalid")

    monkeypatch.setattr(api, "_invoke_runner", _down)
    out = _post_try(api, "198.51.100.20")
    assert out["statusCode"] == 502
    [row] = [row for key, row in table.items.items() if key.startswith("appr-")]
    assert row["status"] == "ERROR"
    assert row["result_text"].startswith("EndpointConnectionError:")


def test_approver_start_prewrites_the_row(modules, monkeypatch):
    common, api, _, table = modules
    handed = []
    monkeypatch.setattr(
        api, "_invoke_runner", lambda payload, asynchronous: handed.append(payload) or {}
    )
    out = api.handler(_event("POST /approvals/start", APPROVER, {"prompt": "do it"}), None)
    assert out["statusCode"] == 202
    approval_id = json.loads(out["body"])["approval_id"]
    assert handed == [{"action": "start", "prompt": "do it", "approval_id": approval_id}]
    assert table.items[approval_id]["lane"] == "approval"
    assert table.items[approval_id]["status"] == "STARTING"
    assert not [key for key in table.items if key.startswith("try-")]


def test_outcome_after_the_sweeper_closed_the_run_is_a_conflict(modules, monkeypatch):
    common, _, runner, table = modules
    _seed_starting("appr-late")

    def _slow(session_id, payload, **kw):
        # The sweeper closes the run while the agent is still working.
        _transition(common, "appr-late", "STARTING", "ERROR", result_text="StartTimeout: x")
        return {"status": "completed", "text": "late"}

    monkeypatch.setattr(common, "invoke_agent", _slow)
    assert runner.start("p", "appr-late") == {"status": "conflict", "approval_id": "appr-late"}
    assert table.items["appr-late"]["status"] == "ERROR"
    assert table.items["appr-late"]["result_text"] == "StartTimeout: x"


def test_sweeper_waits_for_a_claimed_start(modules, monkeypatch):
    common, _, runner, table = modules
    _seed_starting("appr-slow")
    table.items["appr-slow"]["created_ts"] = int(time.time()) - 900
    assert runner.sweep()["closed_starting"] == 0
    table.items["appr-slow"]["created_ts"] = int(time.time()) - runner.STARTING_TIMEOUT_S - 1
    assert runner.sweep()["closed_starting"] == 1
    assert [e["event_type"] for e in _events(table, "appr-slow")][-1] == "RunFailed"


# --- B1 review fixes: contention, hand-offs, ids ----------------------

CONTENDED_START = ["None", "TransactionConflict", "None", "None"]
BUSY = {"error": "busy, try again in a moment"}


def _exhausted():
    return _layer("uow").CONTENTION_RETRIES + 1


def test_contended_try_start_is_retried_then_answers_202(modules, monkeypatch):
    _, api, _, table = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    # The global counter row is in another transaction twice in a row.
    table.dynamo.inject = [CONTENDED_START] * 2
    out = _post_try(api, "198.51.100.30")
    assert out["statusCode"] == 202
    token = json.loads(out["body"])["token"]
    assert table.items[token]["status"] == "STARTING"
    counters = [row["count"] for key, row in table.items.items() if key.startswith("try-")]
    assert sorted(counters) == [1, 1]
    assert len(_events(table, token)) == 1


def test_try_start_under_lasting_contention_is_503_and_consumes_nothing(modules, monkeypatch):
    _, api, _, table = modules
    handed = []
    monkeypatch.setattr(
        api, "_invoke_runner", lambda payload, asynchronous: handed.append(payload) or {}
    )
    table.dynamo.inject = [CONTENDED_START] * _exhausted()
    before = copy.deepcopy(table.dynamo.tables)
    out = _post_try(api, "198.51.100.31")
    assert out["statusCode"] == 503
    assert json.loads(out["body"]) == BUSY
    assert not re.search(r"[0-9]", BUSY["error"])
    assert table.dynamo.tables == before and handed == []


def test_approver_start_under_lasting_contention_is_503(modules, monkeypatch):
    _, api, _, table = modules
    handed = []
    monkeypatch.setattr(
        api, "_invoke_runner", lambda payload, asynchronous: handed.append(payload) or {}
    )
    table.dynamo.inject = [["TransactionConflict", "None"]] * _exhausted()
    out = api.handler(_event("POST /approvals/start", APPROVER, {}), None)
    assert out["statusCode"] == 503
    assert json.loads(out["body"]) == BUSY
    assert not table.items and handed == []


def test_contended_decision_is_503_and_the_run_stays_pending(modules, monkeypatch):
    common, api, _, table = modules
    handed = []
    monkeypatch.setattr(
        api, "_invoke_runner", lambda payload, asynchronous: handed.append(payload) or {}
    )
    _create_row(common, "appr-busy", "s" * 40, "approval")
    _transition(common, "appr-busy", "STARTING", "PENDING", interrupts="[]", card="{}")
    table.dynamo.inject = [["TransactionConflict", "None"]] * _exhausted()
    out = _decide(api, APPROVER, "approve", "appr-busy")
    assert out["statusCode"] == 503
    assert json.loads(out["body"]) == BUSY
    assert table.items["appr-busy"]["status"] == "PENDING" and handed == []
    # The person can simply try again.
    assert _decide(api, APPROVER, "approve", "appr-busy")["statusCode"] == 202


def test_start_hand_off_still_answers_502_when_recording_the_failure_fails(
    modules, monkeypatch
):
    _, api, _, table = modules

    def _down(payload, asynchronous):
        raise EndpointConnectionError(endpoint_url="https://lambda.invalid")

    def _throttled(run, text):
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "TransactWriteItems")

    monkeypatch.setattr(api, "_invoke_runner", _down)
    monkeypatch.setattr(_layer("service"), "record_failure", _throttled)
    out = _post_try(api, "198.51.100.32")
    assert out["statusCode"] == 502
    assert json.loads(out["body"]) == {"error": "the run could not be handed to the runner"}
    [row] = [row for key, row in table.items.items() if key.startswith("appr-")]
    assert row["status"] == "STARTING"  # the sweeper closes it later


@pytest.mark.parametrize("lane", ["approval", "try"])
def test_lost_decision_hand_off_is_202_and_the_sweeper_redispatches(modules, monkeypatch, lane):
    common, api, runner, table = modules

    def _down(payload, asynchronous):
        raise EndpointConnectionError(endpoint_url="https://lambda.invalid")

    monkeypatch.setattr(api, "_invoke_runner", _down)
    approval_id = "appr-lost-" + lane
    _create_row(common, approval_id, "s" * 40, lane)
    _transition(
        common,
        approval_id,
        "STARTING",
        "PENDING",
        interrupts='[{"interruptId": "i1"}]',
        card=json.dumps({"layer": "decider"}),
    )
    if lane == "try":
        decision = _event(
            "POST /try/{token}/decision", {}, {"decision": "approve"}, {"token": approval_id}
        )
        out = api.handler(decision, None)
    else:
        out = _decide(api, APPROVER, "approve", approval_id)
    assert out["statusCode"] == 202
    assert table.items[approval_id]["status"] == "APPROVED"

    dispatched = []
    monkeypatch.setattr(runner, "_invoke_runner", dispatched.append)
    # Too recent: its resume event may still be on the way.
    assert runner.sweep()["redispatched"] == 0 and dispatched == []
    table.items[approval_id]["decided_at"] = "2026-01-01T00:00:00+00:00"
    assert runner.sweep()["redispatched"] == 1
    assert dispatched == [{"action": "resume", "approval_id": approval_id}]

    # The re-dispatched resume runs once; a late duplicate is a no-op.
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: {"status": "completed", "text": "ok"}
    )
    assert runner.handler(dispatched[0], None)["status"] == "DONE"
    assert runner.handler(dispatched[0], None)["status"] == "noop"
    assert runner.sweep()["redispatched"] == 0 and len(dispatched) == 1


def test_sweeper_redispatch_failure_does_not_stop_the_sweep(modules, monkeypatch):
    common, _, runner, _ = modules
    for approval_id, status in (("appr-d1", "APPROVED"), ("appr-d2", "DECLINED")):
        _create_row(common, approval_id, "s" * 40, "approval")
        _transition(common, approval_id, "STARTING", status)  # no decided_at: old
    calls = []

    def _flaky(payload):
        calls.append(payload)
        if len(calls) == 1:
            raise EndpointConnectionError(endpoint_url="https://lambda.invalid")

    monkeypatch.setattr(runner, "_invoke_runner", _flaky)
    assert runner.sweep()["redispatched"] == 1
    assert {call["approval_id"] for call in calls} == {"appr-d1", "appr-d2"}


def test_runner_has_no_sweep_action(modules):
    _, _, runner, _ = modules
    assert runner.handler({"action": "sweep"}, None) == {
        "status": "error",
        "error": "unknown action 'sweep'",
    }


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "try-counter-2026-10-20",
        "try-ip-203.0.113.7-2026-10-20",
        "appr-" + "a" * 3000,
        "appr-<script>",
        "APPR-x",
    ],
)
def test_ids_of_the_wrong_shape_are_404_before_any_read(modules, monkeypatch, bad):
    _, api, _, table = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    table.items["try-counter-2026-10-20"] = {"approval_id": "try-counter-2026-10-20", "count": 3}
    table.reads.clear()
    item_reads = []
    read_item = table.dynamo.get_item
    monkeypatch.setattr(
        table.dynamo, "get_item", lambda **kw: item_reads.append(kw) or read_item(**kw)
    )
    cases = (
        (_event("GET /try/{token}", {}, None, {"token": bad}), "unknown token"),
        (
            _event("POST /try/{token}/decision", {}, {"decision": "approve"}, {"token": bad}),
            "unknown token",
        ),
        (_event("GET /approvals/{id}", APPROVER, None, {"id": bad}), "unknown approval"),
        (_event("GET /try/{token}/events", {}, None, {"token": bad}), "unknown token"),
        (_event("GET /approvals/{id}/events", APPROVER, None, {"id": bad}), "unknown approval"),
        (
            _event("POST /approvals/{id}/decision", APPROVER, {"decision": "approve"}, {"id": bad}),
            "unknown approval",
        ),
    )
    for event, message in cases:
        out = api.handler(event, None)
        assert out["statusCode"] == 404, (event["routeKey"], bad)
        assert json.loads(out["body"]) == {"error": message}
    assert table.reads == [] and item_reads == [] and table.dynamo.transactions == []
    assert table.dynamo.queries == []


def test_real_ids_have_the_checked_shape(modules):
    common, api, _, _ = modules
    for _ in range(50):
        approval_id = common.new_approval_id()
        assert re.fullmatch(r"appr-[A-Za-z0-9_-]{32}", approval_id)
        assert api._valid_id(approval_id)


def test_the_card_carries_the_cached_flag_and_the_event_relays_it(modules):
    """B3: a re-pause built from the handler's re-issued Confirm (cached,
    no latency) is marked, so the audit lake can leave it out of its
    counts of Decider verdicts. The console never sees the flag."""
    common, _, runner, _ = modules
    interrupts = [{"interruptId": "i1", "reason": "r"}]
    fresh = {"tool": "t", "verdict": "confirm", "layer": "decider", "latency_ms": 120.0}
    reissued = {"tool": "t", "verdict": "confirm", "layer": "decider", "latency_ms": None,
                "cached": True}
    assert runner._pending_card({"interrupts": interrupts, "triage": [fresh]})["cached"] is False
    card = runner._pending_card({"interrupts": interrupts, "triage": [reissued]})
    assert card["cached"] is True
    odd = runner._pending_card({"interrupts": interrupts, "triage": [dict(fresh, cached="yes")]})
    assert odd["cached"] is False
    assert "cached" not in common.CARD_KEYS
    domain = _layer("domain")
    run = domain.Run.request("appr-cached", "s" * 40, "approval", time.time(), 1800)
    run.pause(interrupts, card, time.time(), 1800)
    paused = [event for _, event in run.events if event.name == "RunPaused"]
    assert [event.cached for event in paused] == [True]
    run2 = domain.Run.request("appr-fresh", "s" * 40, "approval", time.time(), 1800)
    run2.pause(interrupts, {"cached": "yes"}, time.time(), 1800)
    assert [e.cached for _, e in run2.events if e.name == "RunPaused"] == [False]


# --- B4: the sweeper reads every page, oldest first ---------------------


def _pending_row(common, approval_id, lane, created_ts, expires_ts):
    _create_row(common, approval_id, "s" * 32 + approval_id[-8:].rjust(8, "0"), lane)
    _transition(
        common, approval_id, "STARTING", "PENDING", interrupts='[{"interruptId": "i1"}]', card="{}"
    )
    common._table.items[approval_id].update(created_ts=created_ts, expires_ts=expires_ts)


def _sweep_queries(table, status):
    return [q for q in table.queries if q["ExpressionAttributeValues"][":status"] == status]


def test_sweeper_reads_every_page_of_a_status_oldest_first(modules, monkeypatch):
    """More stale rows than one page: every one is closed, and the reads
    were ascending pages chained by ExclusiveStartKey."""
    common, _, runner, table = modules
    monkeypatch.setattr(common, "SWEEP_PAGE_SIZE", 2)
    for n in range(5):
        _create_row(common, f"appr-stale-{n}", "s" * 40, "try")
        common._table.items[f"appr-stale-{n}"]["created_ts"] = 10 + n
    out = runner.sweep()
    assert out["closed_starting"] == 5
    assert {common.get(f"appr-stale-{n}")["status"] for n in range(5)} == {"ERROR"}
    pages = _sweep_queries(table, "STARTING")
    assert len(pages) == 3
    assert all(q["ScanIndexForward"] is True and q["Limit"] == 2 for q in pages)
    assert "ExclusiveStartKey" not in pages[0]
    assert [q["ExclusiveStartKey"]["approval_id"] for q in pages[1:]] == [
        "appr-stale-1",
        "appr-stale-3",
    ]
    # Every status the sweep handles is read the same way.
    for status in ("RESUMING", "APPROVED", "DECLINED", "PENDING"):
        assert all(q["ScanIndexForward"] is True for q in _sweep_queries(table, status))


def test_sweeper_reaches_due_rows_behind_a_full_page_of_newer_ones(modules, monkeypatch):
    """Regression: the sweep read the newest 100 PENDING rows only. With
    a full page of fresh pauses, older expired ones were never closed."""
    common, _, runner, table = modules
    calls = []
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: calls.append(s) or {})
    now = int(time.time())
    for n in range(100):  # the earlier read's Limit
        _pending_row(common, f"appr-fresh-{n:03d}", "try", now, now + 1800)
    for n in range(3):
        _pending_row(common, f"appr-due-{n}", "try", 1 + n, 1)
    out = runner.sweep()
    assert out["expired"] == 3 and out["closed_try"] == 3
    assert {common.get(f"appr-due-{n}")["status"] for n in range(3)} == {"EXPIRED"}
    assert {
        common.get(f"appr-fresh-{n:03d}")["status"] for n in range(100)
    } == {"PENDING"}
    assert len(_sweep_queries(table, "PENDING")) == 2
    assert calls == []


def test_sweeper_resumes_expired_rows_oldest_first(modules, monkeypatch):
    common, _, runner, _ = modules
    monkeypatch.setattr(common, "SWEEP_PAGE_SIZE", 2)
    sessions = []
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda s, p, **kw: sessions.append(s) or {"status": "completed", "text": "refused"},
    )
    for approval_id, created in (("appr-b", 30), ("appr-c", 10), ("appr-a", 20)):
        _pending_row(common, approval_id, "approval", created, 1)
    runner.sweep()
    by_session = {common.get(a)["session_id"]: a for a in ("appr-a", "appr-b", "appr-c")}
    assert [by_session[s] for s in sessions] == ["appr-c", "appr-a", "appr-b"]
    assert {common.get(a)["status"] for a in by_session.values()} == {"EXPIRED"}


def test_approver_queue_still_reads_newest_first(modules):
    common, api, _, table = modules
    now = int(time.time())
    for approval_id, created in (("appr-old", now - 60), ("appr-new", now)):
        _pending_row(common, approval_id, "approval", created, now + 1800)
    out = api.handler(_event("GET /approvals", claims=APPROVER), None)
    rows = json.loads(out["body"])["approvals"]
    assert [row["approval_id"] for row in rows] == ["appr-new", "appr-old"]
    assert table.queries[-1]["ScanIndexForward"] is False


# ---------------------------------------------------------------------------
# Slice B5: the run ledger (GET /try/{token}/events, GET /approvals/{id}/
# events) and the console views that show it, plus the Architecture view.
# ---------------------------------------------------------------------------

LEDGER_FIELDS = (
    "seq", "type", "at", "status", "lane", "pause_seq",
    "tool", "layer", "risk", "confidence", "latency_ms", "cached",
)
LEDGER_KEYS = set(LEDGER_FIELDS)


def _ledger(api, route, params, claims=None):
    out = api.handler(_event(route, claims or {}, None, params), None)
    return out["statusCode"], out["body"]


def _try_run_paused_then_decided(common, api, runner, monkeypatch):
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    token = json.loads(_post_try(api, "198.51.100.9")["body"])["token"]
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: PAUSE_PAYLOAD)
    assert runner.start("p", token, context=_Lambda("req-ledger-9"))["status"] == "interrupt"
    decide = _event("POST /try/{token}/decision", {}, {"decision": "approve"}, {"token": token})
    assert api.handler(decide, None)["statusCode"] == 202
    return token


def test_try_ledger_returns_the_whitelisted_events_in_order(modules, monkeypatch):
    common, api, runner, table = modules
    token = _try_run_paused_then_decided(common, api, runner, monkeypatch)
    status, raw = _ledger(api, "GET /try/{token}/events", {"token": token})
    assert status == 200
    events = json.loads(raw)["events"]
    assert [e["type"] for e in events] == [
        "RunRequested", "RunClaimed", "RunPaused", "DecisionRecorded"
    ]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert all(type(e["seq"]) is int for e in events)
    assert [e["status"] for e in events] == ["STARTING", "STARTING", "PENDING", "APPROVED"]
    for event in events:
        assert set(event) <= LEDGER_KEYS, set(event) - LEDGER_KEYS
        assert isinstance(event["at"], str) and event["at"]
    assert events[0]["lane"] == "try"
    paused = events[2]
    assert paused["tool"] == "delete_file" and paused["layer"] == "decider"
    assert paused["risk"] == "destructive_write" and paused["cached"] is False
    assert paused["confidence"] == 0.31 and type(paused["confidence"]) is float
    assert paused["latency_ms"] == 187.4 and paused["pause_seq"] == 1
    assert "verdict" not in paused  # no event carries one
    # Never the token, the runner's request id, the visitor marker, the
    # interrupt id, the arguments or the row's own bookkeeping.
    for leak in (token, "req-ledger-9", "try-visitor", "int-secret-7", "prod_secret",
                 "event_id", "run_version", "ttl", "decided_by_name", "request_id",
                 "198.51.100.9", "expires_ts"):
        assert leak not in raw, leak
    # One consistent query, projected to the four top-level attributes it
    # reads and, inside data, to the whitelisted keys only.
    [query] = table.dynamo.queries
    assert query["ConsistentRead"] is True and query["ScanIndexForward"] is True
    names = query["ExpressionAttributeNames"]
    projected = {
        ".".join(names[name] for name in part.strip().split("."))
        for part in query["ProjectionExpression"].split(",")
    }
    data_keys = LEDGER_FIELDS[4:]
    assert projected == {"seq", "event_type", "status", "at"} | {
        f"data.{key}" for key in data_keys
    }
    # The rest of data never leaves DynamoDB for this function.
    assert table.dynamo.returned
    for item in table.dynamo.returned:
        assert set(item) <= {"seq", "event_type", "status", "at", "data"}
        assert set(item.get("data", {})) <= set(data_keys), item


def test_approval_ledger_needs_an_approver_and_hides_who_decided(modules, monkeypatch):
    common, api, runner, table = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    start = api.handler(_event("POST /approvals/start", APPROVER, {}), None)
    approval_id = json.loads(start["body"])["approval_id"]
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: PAUSE_PAYLOAD)
    started = runner.start("p", approval_id, context=_Lambda("req-ledger-a"))
    assert started["status"] == "interrupt"
    assert _decide(api, APPROVER, "decline", approval_id)["statusCode"] == 202

    seen = []
    original = api.ledger.public_event

    def spy(row):
        seen.append(copy.deepcopy(row))
        return original(row)

    monkeypatch.setattr(api.ledger, "public_event", spy)
    params = {"id": approval_id}
    assert _ledger(api, "GET /approvals/{id}/events", params, PLAIN_USER)[0] == 403
    assert _ledger(api, "GET /approvals/{id}/events", params)[0] == 403
    status, raw = _ledger(api, "GET /approvals/{id}/events", params, APPROVER)
    assert status == 200
    events = json.loads(raw)["events"]
    assert [e["type"] for e in events][-1] == "DecisionRecorded"
    assert events[-1]["status"] == "DECLINED" and events[0]["lane"] == "approval"
    for event in events:
        assert set(event) <= LEDGER_KEYS
    for leak in (approval_id, "sub-1", "owner", "req-ledger-a", "decline"):
        assert leak not in raw, leak
    # The stored DecisionRecorded row does name the approver; the query
    # never hands that key, or the runner's request id, to this function.
    stored = [row.get("data", {}) for row in table.dynamo.events_of(approval_id)]
    assert any("decided_by_name" in data for data in stored)
    assert any("request_id" in data for data in stored)
    assert seen and len(seen) == len(events)
    for row in seen:
        for key in ("decided_by_name", "request_id", "answer", "cause", "expires_ts"):
            assert key not in row.get("data", {}), key
        assert "approval_id" not in row and "event_id" not in row


def test_ledger_lanes_are_isolated_and_unknown_runs_are_404(modules, monkeypatch):
    common, api, runner, table = modules
    token = _try_run_paused_then_decided(common, api, runner, monkeypatch)
    _seed_starting("appr-ledger-real", lane="approval")
    table.dynamo.queries.clear()
    cases = (
        ("GET /approvals/{id}/events", {"id": token}, APPROVER, "unknown approval"),
        ("GET /try/{token}/events", {"token": "appr-ledger-real"}, None, "unknown token"),
        ("GET /try/{token}/events", {"token": "appr-ledger-missing"}, None, "unknown token"),
        ("GET /approvals/{id}/events", {"id": "appr-ledger-missing"}, APPROVER,
         "unknown approval"),
    )
    for route, params, claims, message in cases:
        status, raw = _ledger(api, route, params, claims)
        assert status == 404, (route, params)
        assert json.loads(raw) == {"error": message}
    # The lane is checked on the run row before the event log is read.
    assert table.dynamo.queries == []


def test_ledger_reads_every_page_up_to_the_cap(modules, monkeypatch):
    common, api, runner, table = modules
    token = _try_run_paused_then_decided(common, api, runner, monkeypatch)
    monkeypatch.setattr(api.ledger, "PAGE_SIZE", 3)
    events = json.loads(_ledger(api, "GET /try/{token}/events", {"token": token})[1])["events"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    queries = table.dynamo.queries
    assert len(queries) == 2 and "ExclusiveStartKey" not in queries[0]
    assert int(queries[1]["ExclusiveStartKey"]["seq"]) == 3
    table.dynamo.queries.clear()
    monkeypatch.setattr(api.ledger, "PAGE_SIZE", 1)
    monkeypatch.setattr(api.ledger, "MAX_EVENTS", 2)
    events = json.loads(_ledger(api, "GET /try/{token}/events", {"token": token})[1])["events"]
    assert [e["seq"] for e in events] == [1, 2] and len(table.dynamo.queries) == 2


def test_a_failed_ledger_read_is_503_without_detail(modules, monkeypatch, capsys):
    common, api, runner, table = modules
    _seed_starting("appr-ledger-down", lane="try")

    def broken(**kwargs):
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "appr-ledger-down detail"}},
            "Query",
        )

    monkeypatch.setattr(table.dynamo, "query", broken)
    status, raw = _ledger(api, "GET /try/{token}/events", {"token": "appr-ledger-down"})
    assert status == 503
    assert json.loads(raw) == {"error": "the run ledger could not be read"}
    logged = capsys.readouterr().out
    assert "ClientError" in logged and "appr-ledger-down" not in logged

    # A failed read of the run row is 503 too, on both lanes, and the
    # event log is never read.
    _seed_starting("appr-ledger-row", lane="approval")
    table.dynamo.queries.clear()

    def row_down(**kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                       "Message": "appr-ledger-row detail"}},
            "GetItem",
        )

    monkeypatch.setattr(table, "get_item", row_down)
    for route, params, claims in (
        ("GET /try/{token}/events", {"token": "appr-ledger-down"}, None),
        ("GET /approvals/{id}/events", {"id": "appr-ledger-row"}, APPROVER),
    ):
        status, raw = _ledger(api, route, params, claims)
        assert status == 503, route
        assert json.loads(raw) == {"error": "the run ledger could not be read"}
    logged = capsys.readouterr().out
    assert logged.count("ClientError") == 2 and "appr-ledger" not in logged


def test_ledger_entry_drops_bad_fields_and_unknown_rows(modules):
    _, api, _, _ = modules
    public_event = api.ledger.public_event
    row = {
        "approval_id": "appr-x", "seq": Decimal(5), "event_type": "RunPaused",
        "status": "PENDING", "at": "2026-10-20T10:00:00+00:00", "ttl": Decimal(9),
        "data": {
            "tool": ["not", "text"], "layer": "decider", "confidence": Decimal("NaN"),
            "latency_ms": None, "risk": "benign", "cached": "yes", "pause_seq": Decimal(1),
            "expires_ts": Decimal(99), "decided_by_name": "owner",
        },
    }
    assert public_event(row) == {
        "seq": 5, "type": "RunPaused", "status": "PENDING",
        "at": "2026-10-20T10:00:00+00:00", "layer": "decider", "risk": "benign",
        "pause_seq": 1,
    }
    assert public_event({**row, "event_type": "RunDeleted"}) is None
    assert public_event({**row, "seq": Decimal("1.5")}) is None
    assert public_event({**row, "seq": Decimal(0)}) is None
    assert public_event({**row, "seq": True}) is None
    # The ledger's whitelist is the publisher's, plus nothing.
    assert api.ledger.FIELDS == LEDGER_FIELDS


def test_stack_routes_and_grants_the_ledger_exactly():
    """infra/stacks/approvals.py cannot be synthesized here (no CDK); the
    routes tuple and the event-log grants are read from its source."""
    import ast

    stack = Path(__file__).resolve().parents[1] / "infra" / "stacks" / "approvals.py"
    source = stack.read_text(encoding="utf-8")
    routes = None
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "routes"
        ):
            routes = ast.literal_eval(node.value)
    assert routes is not None
    assert ("GET /try/{token}/events", "NONE") in routes
    assert ("GET /approvals/{id}/events", "JWT") in routes
    assert len(routes) == len(set(routes)) == 13
    # B7a: each project read has an anonymous and an approver route.
    for route in (
        ("GET /projects", "NONE"),
        ("GET /projects/{key}/activity", "NONE"),
        ("GET /approvals/projects", "JWT"),
        ("GET /approvals/projects/{key}/activity", "JWT"),
    ):
        assert route in routes, route
    # The handler serves exactly the routes the API declares.
    api_source = (LAMBDA_DIR / "api.py").read_text(encoding="utf-8")
    for route_key, _ in routes:
        assert f'route == "{route_key}"' in api_source, route_key
    grants = re.findall(r"events_table\.grant\((\w+), ([^)]*)\)", source)
    assert sorted(grants) == [("api_fn", '"dynamodb:Query"'), ("fn", '"dynamodb:PutItem"')]
    assert "events_table.grant_read" not in source and "grant_full_access" not in source
    # The anonymous ledger route has its own stage throttle beside POST /try.
    settings = source[source.index('"RouteSettings",'):source.index("for route_resource in")]
    assert '"POST /try": {"ThrottlingRateLimit": 0.2, "ThrottlingBurstLimit": 2}' in settings
    assert '"GET /try/{token}/events": {' in settings
    assert '"ThrottlingRateLimit": 10,' in settings
    assert '"ThrottlingBurstLimit": 20,' in settings


def _section(html: str, view: str) -> str:
    start = html.index(f'<section data-view="{view}"')
    return html[start:html.index("\n    </section>", start)]


def test_console_architecture_view_is_static_and_numberless():
    html = _console("index.html")
    assert 'data-nav="architecture" href="#/architecture"' in html
    nav = html[html.index('data-nav="architecture"'):]
    assert "nav-badge" not in nav[:nav.index("</a>")]
    arch = _section(html, "architecture")
    for service in (
        "Amazon Bedrock AgentCore Runtime", "Strands Decider 2B on Amazon SageMaker",
        "Strands InterventionHandler", "Amazon API Gateway HTTP API and AWS Lambda",
        "Amazon DynamoDB TransactWriteItems", "lachesis-approvals", "lachesis-run-events",
        "AWS Lambda on DynamoDB Streams", "Amazon EventBridge",
        "Amazon Data Firehose to Amazon S3", "AWS Glue and Amazon Athena", "Amazon SQS",
        "Amazon CloudWatch to Amazon SNS", "AWS Step Functions",
    ):
        assert service in arch, service
    assert "Planned: behind a flag, not yet live" in arch
    assert "not a live status page" in arch and "plus one planned part, marked as planned" in arch
    # A deterministic catch never reads as Decider work, and the runner,
    # not the API, invokes the agent and records the pause.
    assert "Runs the deterministic pre-checks first" in arch
    assert "for every other call it asks Decider two typed questions" in arch
    assert "runner Lambda, which invokes the agent and records each pause" in arch
    # Every node names its service and its one job.
    nodes = arch.split('<li class="arch-node')[1:]
    assert len(nodes) == 12
    for node in nodes:
        assert '<span class="arch-svc">' in node and node.count("<span>") == 1
    # No numbers on this view: only the digits inside known names.
    stripped = arch.replace("Decider 2B", "").replace("Amazon S3", "")
    assert not re.search(r"\d", re.sub(r"<[^>]+>", "", stripped))
    assert "<script" not in arch and "<svg" not in arch
    js = _console("app.js")
    assert 'p[0] === "architecture" ? "architecture"' in js
    assert ".arch-node" in js[js.index("function fx(view)"):]


def test_console_run_ledger_renders_beside_the_step_timeline():
    html = _console("index.html")
    run = html[html.index('<div id="run" hidden>'):html.index('<section data-view="queue"')]
    assert run.index('<ol id="steps"') < run.index('<section id="run-ledger"')
    detail = _section(html, "detail")
    assert detail.index('<div id="detail-body"></div>') < detail.index(
        '<section id="detail-ledger"'
    )
    js = _console("app.js")
    block = js[js.index("function ledgerFacts(e)"):js.index("function viewTry(token)")]
    # The ledger never touches the step timeline or the decision card,
    # and every value reaches the DOM through h(), that is textContent.
    assert "#steps" not in block and "#detail-body" not in block
    assert "innerHTML" not in block and 'el.textContent = ""' in block
    for fact in ('"tool "', '"layer "', '"verdict "', '"confidence "', "re-issued pause"):
        assert fact in block, fact
    # A score only on a Decider row, through scored(): a fail-closed,
    # secret-scan or refusal row carries a placeholder confidence (0.0 or
    # 1.0) that is never a Decider score, and reads "no Decider score".
    facts = block[:block.index("function renderLedger")]
    gate, other = facts.index("if (scored(e)) {"), facts.index("} else if (e.layer)")
    assert gate < facts.index('"confidence "') < other
    assert gate < facts.index('" ms"') < other
    assert 'else if (e.layer) facts.push("no Decider score");' in facts
    assert "typeof e.confidence" not in facts
    # Empty hides it; a failed, stale or superseded read changes nothing,
    # and a failed read at a terminal status is retried once.
    assert "el.hidden = !Array.isArray(events) || !events.length;" in block
    assert "n = el._req = (el._req || 0) + 1;" in block
    assert "if (g !== gen || n !== el._req) return;" in block
    assert "if (out.status === 200 && out.body) renderLedger(el, out.body.events);" in block
    assert (
        "else if (terminal) later(() => { if (g === gen && n === el._req) "
        "loadLedger(path, el, false); }, 3000);" in block
    )
    tail = ", led, TERMINAL.includes(v.status))"
    assert 'loadLedger("/try/" + encodeURIComponent(token) + "/events"' + tail in js
    assert 'loadLedger("/approvals/" + encodeURIComponent(id) + "/events"' + tail in js
    view = js[js.index("function viewTry(token)"):]
    assert view.index("led.hidden = true;") < view.index("if (!token)")
    assert 'href="app.css?v=20261023a"' in html

"""Approvals logic tests (B1 acceptance, offline): a repeat decision is
refused, a non-approver gets 403, a duplicate resume resumes once, and
the /try daily counter stops at the cap. DynamoDB is an in-memory fake
implementing exactly the conditional writes common.py uses.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "approvals" / "lambda"
sys.path.insert(0, str(LAMBDA_DIR))


class FakeTable:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803
        key = Item["approval_id"]
        if ConditionExpression and key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
            )
        self.items[key] = dict(Item)
        return {}

    def get_item(self, Key):  # noqa: N803
        item = self.items.get(Key["approval_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None, **kwargs):  # noqa: N803
        key = Key["approval_id"]
        item = self.items.get(key)
        values = kwargs.get("ExpressionAttributeValues", {})
        names = kwargs.get("ExpressionAttributeNames", {})
        if ConditionExpression == "#s = :expected" and (
            item is None or item.get("status") != values[":expected"]
        ):
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
            )
        if item is None:
            item = {"approval_id": key}
            self.items[key] = item
        if "if_not_exists" in UpdateExpression:
            current = int(item.get("count", 0))
            over_cap = "attribute_not_exists(#c) OR #c < :cap" in (ConditionExpression or "")
            if over_cap and current >= int(values[":cap"]):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "UpdateItem",
                )
            item["count"] = current + 1
            return {"Attributes": dict(item)}
        assignments = UpdateExpression.removeprefix("SET ").split(", ")
        for assignment in assignments:
            lhs, rhs = [part.strip() for part in assignment.split("=", 1)]
            field = names.get(lhs, lhs)
            item[field] = values[rhs]
        return {"Attributes": dict(item)}

    def query(self, IndexName, KeyConditionExpression, **kwargs):  # noqa: N803
        status = kwargs["ExpressionAttributeValues"][":status"]
        rows = [dict(i) for i in self.items.values() if i.get("status") == status]
        rows.sort(key=lambda i: int(i.get("created_ts", 0)), reverse=True)
        return {"Items": rows[: kwargs.get("Limit", 50)]}


@pytest.fixture()
def modules(monkeypatch):
    table = FakeTable()
    spec = importlib.util.spec_from_file_location("common", LAMBDA_DIR / "common.py")
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    monkeypatch.setattr(common, "_table", table)
    for name in ("api", "runner"):
        spec = importlib.util.spec_from_file_location(name, LAMBDA_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "common", common)
    return common, sys.modules["api"], sys.modules["runner"], table


def _event(route, claims=None, body=None, params=None):
    return {
        "routeKey": route,
        "pathParameters": params or {},
        "body": json.dumps(body or {}),
        "requestContext": {"authorizer": {"jwt": {"claims": claims or {}}}},
    }


APPROVER = {"sub": "sub-1", "username": "owner", "cognito:groups": "approvers"}
PLAIN_USER = {"sub": "sub-2", "username": "guest", "cognito:groups": ""}


def test_create_and_transition_guard(modules):
    common, _, _, table = modules
    common.create_row("appr-1", "s" * 40, "approval")
    common.transition("appr-1", "STARTING", "PENDING", interrupts="[]", card="{}")
    moved = common.transition("appr-1", "PENDING", "APPROVED", decided_by="sub-1")
    assert moved["status"] == "APPROVED"
    assert common.transition("appr-1", "PENDING", "DECLINED") is None


def _decide(api, claims, decision, approval_id):
    event = _event(
        "POST /approvals/{id}/decision", claims, {"decision": decision}, {"id": approval_id}
    )
    return api.handler(event, None)


def test_decision_repeat_is_409_and_non_approver_403(modules, monkeypatch):
    common, api, _, _ = modules
    common.create_row("appr-2", "s" * 40, "approval")
    common.transition("appr-2", "STARTING", "PENDING", interrupts="[]", card="{}")
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    assert _decide(api, PLAIN_USER, "approve", "appr-2")["statusCode"] == 403
    assert _decide(api, APPROVER, "approve", "appr-2")["statusCode"] == 202
    assert _decide(api, APPROVER, "decline", "appr-2")["statusCode"] == 409


def test_resume_only_once(modules, monkeypatch):
    common, _, runner, _ = modules
    common.create_row("appr-3", "s" * 40, "approval")
    interrupts = '[{"interruptId": "i1"}]'
    common.transition("appr-3", "STARTING", "PENDING", interrupts=interrupts, card="{}")
    common.transition("appr-3", "PENDING", "APPROVED", decided_by="sub-1")
    calls = []
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda session_id, payload: calls.append(payload)
        or {"status": "completed", "text": "done"},
    )
    assert runner.resume("appr-3")["status"] == "DONE"
    assert runner.resume("appr-3")["status"] == "noop"
    assert len(calls) == 1


def test_resume_error_returns_row_to_pending(modules, monkeypatch):
    common, _, runner, _ = modules
    common.create_row("appr-4", "s" * 40, "approval")
    interrupts = '[{"interruptId": "i1"}]'
    common.transition("appr-4", "STARTING", "PENDING", interrupts=interrupts, card="{}")
    common.transition("appr-4", "PENDING", "APPROVED", decided_by="sub-1")

    def _boom(session_id, payload):
        raise RuntimeError("runtime 500")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    assert runner.resume("appr-4")["status"] == "error-resumable"
    assert common.get("appr-4")["status"] == "PENDING"


def test_start_card_carries_decider_fields(modules, monkeypatch):
    common, _, runner, _ = modules
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda session_id, payload: {
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
        },
    )
    out = runner.start("prompt", "try", "appr-card")
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
    assert card["expires_at"]


def test_resume_repause_card_keeps_decider_fields(modules, monkeypatch):
    common, _, runner, _ = modules
    common.create_row("appr-5", "s" * 40, "approval")
    interrupts = '[{"interruptId": "i1"}]'
    common.transition("appr-5", "STARTING", "PENDING", interrupts=interrupts, card="{}")
    common.transition("appr-5", "PENDING", "APPROVED", decided_by="sub-1")
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda session_id, payload: {
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

    card = json.loads(common.get("appr-5")["card"])
    assert card["tool"] == "run_tests"
    assert card["risk"] == "destructive_write"
    assert card["confidence"] == 0.24
    assert card["latency_ms"] == 171.0
    assert card["layer"] == "decider"
    assert card["expires_at"]


def test_empty_try_token_is_404(modules):
    _, api, _, _ = modules
    out = api.handler(_event("GET /try/{token}", {}, None, {"token": ""}), None)
    assert out["statusCode"] == 404


def test_expired_decision_is_409(modules, monkeypatch):
    common, api, _, _ = modules
    common.create_row("appr-old", "s" * 40, "approval")
    common.transition("appr-old", "STARTING", "PENDING", interrupts="[]", card="{}")
    common._table.items["appr-old"]["expires_ts"] = 1
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    out = _decide(api, APPROVER, "approve", "appr-old")
    assert out["statusCode"] == 409


def test_approver_queue_hides_try_rows(modules):
    common, api, _, _ = modules
    common.create_row("appr-q1", "s" * 40, "approval")
    common.transition("appr-q1", "STARTING", "PENDING", interrupts="[]", card="{}")
    common.create_row("appr-q2", "s" * 40, "try")
    common.transition("appr-q2", "STARTING", "PENDING", interrupts="[]", card="{}")
    body = api.handler(_event("GET /approvals", APPROVER), None)
    import json

    ids = [a["approval_id"] for a in json.loads(body["body"])["approvals"]]
    assert ids == ["appr-q1"]


def test_start_failure_lands_in_error(modules, monkeypatch):
    common, _, runner, _ = modules

    def _boom(session_id, payload):
        raise RuntimeError("agent down")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    out = runner.start("prompt", "try", "appr-err")
    assert out["status"] == "error"
    assert common.get("appr-err")["status"] == "ERROR"


def test_sweeper_closes_stale_starting_and_caps_attempts(modules, monkeypatch):
    common, _, runner, _ = modules
    common.create_row("appr-stale", "s" * 40, "try")
    common._table.items["appr-stale"]["created_ts"] = 1
    monkeypatch.setattr(common, "invoke_agent", lambda s, p: {"status": "completed", "text": ""})
    out = runner.sweep()
    assert out["closed_starting"] == 1
    assert common.get("appr-stale")["status"] == "ERROR"

    row = {"approval_id": "appr-x", "sweep_attempts": 2}
    common.create_row("appr-x", "s" * 40, "approval")
    common.transition("appr-x", "STARTING", "PENDING", interrupts="[]", card="{}")
    common.transition("appr-x", "PENDING", "APPROVED")
    common.transition("appr-x", "APPROVED", "RESUMING")
    assert runner._attempt_failed(row) == "ERROR"
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
        common, "invoke_agent", lambda s, p: calls.append(p) or {"status": "completed"}
    )
    for aid, lane in (("appr-try-old", "try"), ("appr-appr-old", "approval")):
        common.create_row(aid, "s" * 40, lane)
        common.transition(aid, "STARTING", "PENDING", interrupts="[]", card="{}")
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


def test_console_try_view_has_meta_banner_and_error_terminal():
    html = (
        Path(__file__).resolve().parents[1]
        / "approvals/console/index.html"
    ).read_text(encoding="utf-8")
    assert "function cardMeta" in html
    assert "const { meta, confidence, reason } = cardMeta(card);" in html
    assert "function priorStepsText" in html
    assert "function coldBanner" in html
    assert "NEW_PASSWORD_REQUIRED" in html
    assert "The Decider GPU is cold" in html
    assert '"EXPIRED", "ERROR"' in html
    assert "paste a staging key" not in html


def test_try_counter_cap(modules, monkeypatch):
    common, api, _, _ = modules
    monkeypatch.setattr(common, "TRY_DAILY_CAP", 2)
    monkeypatch.setattr(common, "TRY_IP_DAILY_CAP", 5)
    event = {"requestContext": {"http": {"sourceIp": "203.0.113.7"}}}
    assert api._try_counter_ok(event) is True
    assert api._try_counter_ok(event) is True
    assert api._try_counter_ok(event) is False


def test_try_per_ip_cap(modules, monkeypatch):
    common, api, _, _ = modules
    monkeypatch.setattr(common, "TRY_DAILY_CAP", 100)
    monkeypatch.setattr(common, "TRY_IP_DAILY_CAP", 1)
    event = {"requestContext": {"http": {"sourceIp": "203.0.113.9"}}}
    assert api._try_counter_ok(event) is True
    assert api._try_counter_ok(event) is False

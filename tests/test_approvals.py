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


def test_try_counter_cap(modules, monkeypatch):
    common, api, _, _ = modules
    monkeypatch.setattr(common, "TRY_DAILY_CAP", 2)
    assert api._try_counter_ok() is True
    assert api._try_counter_ok() is True
    assert api._try_counter_ok() is False

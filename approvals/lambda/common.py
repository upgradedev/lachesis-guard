"""Shared approval-store logic for the Lachesis Approvals Lambdas.

One DynamoDB table, one row per approval:

    approval_id   (PK)  unguessable id, also the /try token
    session_id          AgentCore runtime session (33+ chars)
    status              STARTING | PENDING | APPROVED | DECLINED | RESUMING |
                        DONE | EXPIRED | REFUSED_DONE | ERROR
    card                {tool, reason, risk, confidence, ..., expires_at}
    interrupts          [{interruptId, name, reason}]
    decided_by          Cognito sub of the latest approver (never an email)
    decided_by_name     Cognito username of the latest approver
    decided_at          when the latest decision was recorded
    decisions           [{decision, decided_by_name, at}], one entry per
                        decision already consumed by a resume that paused
                        again, so a re-pause never erases who decided first
    result_text         agent's final text after a resume; on ERROR,
                        "<TypeName>: <detail>" (only TypeName is public)
    lane                "approval" | "try"

Every state change is a conditional write on the expected current
status: a repeat decision gets 409, a duplicate resume event resumes
once, and an expired row is resumed with "no" by the sweeper. GET
never approves.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import time

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
)

TABLE = os.environ.get("APPROVALS_TABLE", "lachesis-approvals")
RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
REGION = os.environ.get("AWS_REGION", "eu-west-1")
TTL_SECONDS = int(os.environ.get("APPROVAL_TTL_SECONDS", "1800"))
TRY_DAILY_CAP = int(os.environ.get("TRY_DAILY_CAP", "100"))
TRY_IP_DAILY_CAP = int(os.environ.get("TRY_IP_DAILY_CAP", "5"))

_ddb = boto3.resource("dynamodb", region_name=REGION)
_table = _ddb.Table(TABLE)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def new_approval_id() -> str:
    return "appr-" + secrets.token_urlsafe(24)


def new_session_id() -> str:
    return "lachesis-" + secrets.token_urlsafe(24)  # 41 chars, >= 33


def put_pending(approval_id: str, session_id: str, interrupts: list, lane: str) -> dict:
    created = time.time()
    card = {
        "reason": interrupts[0].get("reason", "") if interrupts else "",
        "created_at": now_iso(),
        "expires_at": datetime.datetime.fromtimestamp(
            created + TTL_SECONDS, datetime.timezone.utc
        ).isoformat(timespec="seconds"),
    }
    item = {
        "approval_id": approval_id,
        "session_id": session_id,
        "status": "PENDING",
        "lane": lane,
        "card": json.dumps(card),
        "interrupts": json.dumps(interrupts),
        "created_ts": int(created),
        "expires_ts": int(created + TTL_SECONDS),
        "ttl": int(created + TTL_SECONDS + 86400),
    }
    _table.put_item(Item=item, ConditionExpression="attribute_not_exists(approval_id)")
    return item


def create_row(approval_id: str, session_id: str, lane: str) -> dict:
    """The STARTING row written before the agent is invoked, so callers
    can return the token at once and poll for the outcome."""
    created = int(time.time())
    item = {
        "approval_id": approval_id,
        "session_id": session_id,
        "status": "STARTING",
        "lane": lane,
        "card": "{}",
        "interrupts": "[]",
        "created_ts": created,
        "expires_ts": created + TTL_SECONDS,
        "ttl": created + TTL_SECONDS + 86400,
    }
    _table.put_item(Item=item, ConditionExpression="attribute_not_exists(approval_id)")
    return item


def get(approval_id: str) -> dict | None:
    response = _table.get_item(Key={"approval_id": approval_id})
    return response.get("Item")


def transition(approval_id: str, expected: str, target: str, **fields) -> dict | None:
    """Conditional status move; None when the row is not in `expected`."""
    names = {"#s": "status"}
    values = {":expected": expected, ":target": target}
    sets = ["#s = :target"]
    for index, (key, value) in enumerate(fields.items()):
        names[f"#f{index}"] = key
        values[f":v{index}"] = value
        sets.append(f"#f{index} = :v{index}")
    try:
        response = _table.update_item(
            Key={"approval_id": approval_id},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="#s = :expected",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return response["Attributes"]


def list_by_status(status: str, limit: int = 50) -> list[dict]:
    response = _table.query(
        IndexName="status-index",
        KeyConditionExpression="#s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": status},
        ScanIndexForward=False,
        Limit=limit,
    )
    return response.get("Items", [])


# One attempt, no retries. An agent invocation is not idempotent: the
# botocore default retries a ReadTimeoutError, which would run the agent
# a second time, and it would also make the final exception say nothing
# about whether an earlier attempt already reached the agent. With a
# single attempt, the exception type alone says whether the request was
# sent (see failed_before_send). read_timeout is left at the botocore
# default until real run durations size it.
AGENT_CLIENT_CONFIG = Config(retries={"total_max_attempts": 1, "mode": "standard"})

# Exceptions raised before any byte of the request reached the service.
# Matched by exact class membership, never by a base class: botocore's
# ReadTimeoutError and ConnectionClosedError mean the request was sent.
PRE_SEND_ERRORS: tuple[type[BaseException], ...] = (
    EndpointConnectionError,
    ConnectTimeoutError,
    ParamValidationError,
    NoCredentialsError,
)


def failed_before_send(exc: BaseException) -> bool:
    """True only when ``exc`` proves the agent was never invoked.

    Anything else (a read timeout, a closed connection, any ClientError
    including 4xx, a body that is not JSON) may have run the agent, so
    the caller must treat the outcome as unknown.
    """
    return type(exc) in PRE_SEND_ERRORS


def invoke_agent(session_id: str, payload: dict) -> dict:
    client = boto3.client("bedrock-agentcore", region_name=REGION, config=AGENT_CLIENT_CONFIG)
    response = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
    )
    return json.loads(response["response"].read())


def error_text(exc: BaseException) -> str:
    """The stored ERROR text: type name first, so public_view can cut it."""
    return f"{type(exc).__name__}: {exc}"


CARD_KEYS = (
    "reason",
    "tool",
    "risk",
    "confidence",
    "latency_ms",
    "layer",
    "prior_steps",
    "changed_files",
    "created_at",
    "expires_at",
)
STEP_KEYS = ("tool", "verdict", "risk", "confidence", "grounded", "latency_ms", "layer")
DECISION_KEYS = ("decision", "decided_by_name", "at")
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")


def error_label(text: str) -> str:
    """Only the exception type name of a stored error: the text before the
    first colon, and only when it looks like an identifier."""
    if not text:
        return ""
    prefix = str(text).split(":", 1)[0].strip()
    return prefix if _TYPE_NAME.fullmatch(prefix) else "Error"


def _public_card(raw: str) -> dict:
    try:
        card = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(card, dict):
        return {}
    view = {key: card[key] for key in CARD_KEYS if key in card}
    if "prior_steps" in view:
        steps = view["prior_steps"] if isinstance(view["prior_steps"], list) else []
        view["prior_steps"] = [
            {key: step[key] for key in STEP_KEYS if key in step}
            for step in steps
            if isinstance(step, dict)
        ]
    return view


def public_view(item: dict) -> dict:
    """What the console and /try may see, as an explicit whitelist.

    Never the Cognito sub (decided_by), the session id, the raw
    interrupts, or raw exception text: on ERROR only the exception type
    name is returned. The card is cut to CARD_KEYS. Its ``reason`` is the
    Confirm prompt the person decides on, which by design names the tool
    and shows its arguments.
    """
    status = item["status"]
    result_text = str(item.get("result_text", "") or "")
    decisions = item.get("decisions") if isinstance(item.get("decisions"), list) else []
    return {
        "approval_id": item["approval_id"],
        "status": status,
        "lane": item.get("lane", "approval"),
        "card": _public_card(item.get("card") or "{}"),
        "decided_by_name": item.get("decided_by_name", ""),
        "decisions": [
            {key: str(entry.get(key, "")) for key in DECISION_KEYS}
            for entry in decisions
            if isinstance(entry, dict)
        ],
        "result_text": error_label(result_text) if status == "ERROR" else result_text,
        "created_ts": int(item.get("created_ts", 0)),
    }

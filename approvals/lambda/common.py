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
                        again, so a re-pause never erases who decided first;
                        "approve_not_sent" / "decline_not_sent" mark a
                        decision whose resume failed before it was sent
    result_text         agent's final text after a resume; on ERROR,
                        "<TypeName>: <detail>" (only TypeName is public)
    lane                "approval" | "try"
    version             per-run integer, +1 per committed command
    pause_seq           how many times the run has paused
    event_seq           seq of the run's last event in lachesis-run-events
    claimed_request_id  Lambda request id of the runner that claimed it
    resumed_decision    APPROVED | DECLINED | "" while RESUMING

The row is the Run aggregate of approval/domain.py. Every state change
is one command of approval/service.py, written by the Unit of Work
(approval/uow.py) as one TransactWriteItems conditioned on the status
and version the run was loaded at, together with its domain events: a
repeat decision gets 409, a duplicate resume event resumes once, a
duplicate start event is claimed once, and an expired row is resumed
with "no" by the sweeper. This module keeps the reads, the agent client
and the public view. GET never approves.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets

import boto3
from approval.domain import error_label
from botocore.config import Config
from botocore.exceptions import (
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

EVENTS_TABLE = os.environ.get("RUN_EVENTS_TABLE", "lachesis-run-events")

_ddb = boto3.resource("dynamodb", region_name=REGION)
_table = _ddb.Table(TABLE)
# The resource's client: TransactWriteItems with plain Python values
# (approval/uow.py). Its high-level layer serializes them; floats are
# refused, so the Unit of Work converts them to Decimal.
_client = _ddb.meta.client


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def new_approval_id() -> str:
    return "appr-" + secrets.token_urlsafe(24)


def new_session_id() -> str:
    return "lachesis-" + secrets.token_urlsafe(24)  # 41 chars, >= 33


def get(approval_id: str) -> dict | None:
    # Strongly consistent: a token answered with 202 a moment ago must be
    # readable now (the start transaction has committed by then).
    response = _table.get_item(Key={"approval_id": approval_id}, ConsistentRead=True)
    return response.get("Item")


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
# sent (see failed_before_send).
#
# The timeouts are explicit, never the botocore 60 s defaults. The agent
# container answers only when its run ends, so read_timeout bounds a
# whole start or resume. AGENT_READ_TIMEOUT is the ceiling, kept under
# the runner Lambda's 900 s limit; callers that know their Lambda's
# remaining time pass a smaller value (runner._read_timeout). No start or
# resume duration near the ceiling has been measured.
AGENT_CONNECT_TIMEOUT = 10
AGENT_READ_TIMEOUT = 840


def agent_client_config(read_timeout: float = AGENT_READ_TIMEOUT) -> Config:
    return Config(
        retries={"total_max_attempts": 1, "mode": "standard"},
        connect_timeout=AGENT_CONNECT_TIMEOUT,
        read_timeout=read_timeout,
    )


AGENT_CLIENT_CONFIG = agent_client_config()

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


def invoke_agent(session_id: str, payload: dict, read_timeout: float | None = None) -> dict:
    config = AGENT_CLIENT_CONFIG if read_timeout is None else agent_client_config(read_timeout)
    client = boto3.client("bedrock-agentcore", region_name=REGION, config=config)
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


# The argument keys the anonymous /try lane may see, per tool. The Confirm
# prompt embeds the full arguments JSON; on the try lane public_view
# rewrites that block to these keys, with string values capped, so a
# token holder never receives an argument this list does not name.
TRY_ARG_KEYS: dict[str, tuple[str, ...]] = {
    "write_file": ("path", "content"),
    "delete_file": ("path",),
    "read_file": ("path",),
    "run_tests": (),
}
TRY_ARG_CHARS = 200
_ARGS_MARK = "\narguments:\n"
_TOOL_LINE = re.compile(r"^tool: (\S*)$", re.MULTILINE)


def _try_reason(reason: str, card_tool: str) -> str:
    """The Confirm prompt with its arguments block cut to TRY_ARG_KEYS.

    The block is split exactly as the console's parseReason splits it:
    after the arguments marker, up to the last blank line. Arguments that
    are not a JSON object (a capped read, for example) become {}.
    """
    start = reason.find(_ARGS_MARK)
    if start < 0:
        return reason
    head, rest = reason[: start + len(_ARGS_MARK)], reason[start + len(_ARGS_MARK):]
    cut = rest.rfind("\n\n")
    args_text, tail = (rest[:cut], rest[cut:]) if cut >= 0 else (rest, "")
    match = _TOOL_LINE.search(head)
    tool = match.group(1) if match else card_tool
    try:
        args = json.loads(args_text)
    except (TypeError, ValueError):
        args = None
    kept = {}
    if isinstance(args, dict):
        for key in TRY_ARG_KEYS.get(tool, ()):
            if key not in args:
                continue
            value = args[key]
            if isinstance(value, str) and len(value) > TRY_ARG_CHARS:
                value = value[:TRY_ARG_CHARS] + "..."
            elif not isinstance(value, (str, int, float, bool)) and value is not None:
                continue
            kept[key] = value
    return head + json.dumps(kept, ensure_ascii=False, indent=2) + tail


def public_view(item: dict) -> dict:
    """What the console and /try may see, as an explicit whitelist.

    Never the Cognito sub (decided_by), the session id, the raw
    interrupts, or raw exception text: on ERROR only the exception type
    name is returned. The card is cut to CARD_KEYS. Its ``reason`` is the
    Confirm prompt the person decides on, which names the tool and shows
    its arguments; on the try lane those arguments are cut to
    TRY_ARG_KEYS (see _try_reason).

    decided_by_name is the person who decided the current pause, so it
    is empty while the row is PENDING (a re-pause keeps the earlier name
    in the row) and on EXPIRED (nobody decided the last pause). Earlier
    decisions are in ``decisions``.
    """
    status = item["status"]
    lane = item.get("lane", "approval")
    result_text = str(item.get("result_text", "") or "")
    decisions = item.get("decisions") if isinstance(item.get("decisions"), list) else []
    card = _public_card(item.get("card") or "{}")
    if lane == "try" and isinstance(card.get("reason"), str):
        card["reason"] = _try_reason(card["reason"], str(card.get("tool", "")))
    decided_by_name = "" if status in {"PENDING", "EXPIRED"} else item.get("decided_by_name", "")
    return {
        "approval_id": item["approval_id"],
        "status": status,
        "lane": lane,
        "card": card,
        "decided_by_name": decided_by_name,
        "decisions": [
            {key: str(entry.get(key, "")) for key in DECISION_KEYS}
            for entry in decisions
            if isinstance(entry, dict)
        ],
        "result_text": error_label(result_text) if status == "ERROR" else result_text,
        "created_ts": int(item.get("created_ts", 0)),
    }

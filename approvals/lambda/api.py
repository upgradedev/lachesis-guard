"""The Approvals HTTP API Lambda (B1) plus the anonymous /try lane (B2).

Routes (HTTP API, see infra/stacks/approvals.py):
- GET  /approvals            approvers group only: pending approver-lane rows
- GET  /approvals/{id}       approvers group only
- POST /approvals/{id}/decision  approvers group only; conditional write,
                             409 on a repeat, 202 + async runner resume
- POST /try                  no auth: starts the canned demo run, returns
                             the unguessable approval id as its token
- GET  /try/{token}          no auth: that run's public view only

Authorization is checked here from the JWT claims the HTTP API's JWT
authorizer already validated: membership in the `approvers` group.
Anything else is 403. The approver is recorded by Cognito `sub` and
username; an email never enters the record (the token carries none).
"""

from __future__ import annotations

import datetime
import json
import os

import boto3
import common
from botocore.exceptions import ClientError

RUNNER_ARN = os.environ.get("RUNNER_ARN", "")
COUNTER_TTL_SECONDS = 2 * 86400
# The governed calls must be ones Decider has to judge (F2): no
# secret-shaped string anywhere, so the deterministic scan stays silent.
# The first call is a benign write whose content the prompt dictates
# word for word, so it is grounded and only Decider can let it proceed;
# the delete that follows is the pause. The secret-scan catch remains
# available as a second, labelled layer (it is what
# scripts/agentcore_smoke.py drives).
CANNED_PROMPT = (
    "Do this now, without asking me anything: first create the file "
    "cleanup_notes.txt with the write_file tool; its entire content "
    "must be exactly this: Workspace check started: reviewing "
    "refunds_prod_config.py status. Then delete the file "
    "refunds_prod_config.py from the workspace with the delete_file "
    "tool, then run the tests with run_tests."
)


def _response(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }


def _claims(event: dict) -> dict:
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )


def _is_approver(event: dict) -> bool:
    groups = _claims(event).get("cognito:groups", "")
    if isinstance(groups, list):
        return "approvers" in groups
    # API Gateway stringifies array claims, e.g. "[approvers]".
    tokens = str(groups).strip("[]").replace('"', "").split(",")
    return "approvers" in [token.strip() for token in tokens]


def _counter_ok(key: str, cap: int) -> bool:
    """One DynamoDB counter row caps a counter per UTC day.

    The row carries a ttl two days out, set on its first write, so the
    table's TTL removes it (and the visitor address in its key) once the
    day it counts is over."""
    import time as _time

    table = common._table
    try:
        response = table.update_item(
            Key={"approval_id": key},
            UpdateExpression=(
                "SET #c = if_not_exists(#c, :zero) + :one, #t = if_not_exists(#t, :ttl)"
            ),
            ConditionExpression="attribute_not_exists(#c) OR #c < :cap",
            ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
            ExpressionAttributeValues={
                ":zero": 0,
                ":one": 1,
                ":cap": cap,
                ":ttl": int(_time.time()) + COUNTER_TTL_SECONDS,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return int(response["Attributes"]["count"]) <= cap


def _try_counter_ok(event: dict) -> bool:
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    source_ip = (
        event.get("requestContext", {}).get("http", {}).get("sourceIp", "unknown")
    )
    if not _counter_ok(f"try-ip-{source_ip}-{day}", common.TRY_IP_DAILY_CAP):
        return False
    return _counter_ok(f"try-counter-{day}", common.TRY_DAILY_CAP)


def _invoke_runner(payload: dict, asynchronous: bool) -> dict:
    client = boto3.client("lambda", region_name=common.REGION)
    response = client.invoke(
        FunctionName=RUNNER_ARN,
        InvocationType="Event" if asynchronous else "RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    if asynchronous:
        return {}
    return json.loads(response["Payload"].read() or b"{}")


def _parse_body(event: dict) -> dict | None:
    raw = event.get("body")
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def handler(event, _context):
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}

    if route == "POST /try":
        if not _try_counter_ok(event):
            return _response(
                429,
                {
                    "error": "daily demo cap reached for this address or overall; "
                    "the counters reset at 00:00 UTC"
                },
            )
        token = common.new_approval_id()
        _invoke_runner(
            {
                "action": "start",
                "prompt": CANNED_PROMPT,
                "lane": "try",
                "approval_id": token,
            },
            asynchronous=True,
        )
        return _response(202, {"token": token, "status": "STARTING"})

    if route == "GET /try/{token}":
        token = params.get("token", "")
        if not token:
            return _response(404, {"error": "unknown token"})
        item = common.get(token)
        if item is None or item.get("lane") != "try":
            return _response(404, {"error": "unknown token"})
        return _response(200, common.public_view(item))

    if route == "POST /try/{token}/decision":
        token = params.get("token", "")
        if not token:
            return _response(404, {"error": "unknown token"})
        body = _parse_body(event)
        if body is None:
            return _response(400, {"error": "request body must be a JSON object"})
        decision = body.get("decision")
        if decision not in {"approve", "decline"}:
            return _response(400, {"error": "decision must be approve or decline"})
        item = common.get(token)
        if item is None or item.get("lane") != "try":
            return _response(404, {"error": "unknown token"})
        import time as _time

        if int(item.get("expires_ts", 0)) and int(item["expires_ts"]) < int(_time.time()):
            return _response(409, {"error": "approval has expired"})
        card = json.loads(item.get("card") or "{}")
        if card.get("layer") == "unavailable":
            return _response(
                409,
                {
                    "error": "Decider GPU is cold (scale-to-zero); "
                    "a fail-closed pause on the public lane cannot be resumed"
                },
            )
        target = "APPROVED" if decision == "approve" else "DECLINED"
        moved = common.transition(
            token,
            "PENDING",
            target,
            decided_by="try-token",
            decided_by_name="try-visitor",
            decided_at=common.now_iso(),
        )
        if moved is None:
            return _response(409, {"error": "approval is no longer pending"})
        _invoke_runner({"action": "resume", "approval_id": token}, asynchronous=True)
        return _response(202, {"token": token, "status": target})

    # Everything below needs an approver.
    if not _is_approver(event):
        return _response(403, {"error": "approvers group membership required"})

    if route == "POST /approvals/start":
        body = _parse_body(event)
        if body is None:
            return _response(400, {"error": "request body must be a JSON object"})
        token = common.new_approval_id()
        _invoke_runner(
            {
                "action": "start",
                "prompt": str(body.get("prompt") or CANNED_PROMPT),
                "lane": "approval",
                "approval_id": token,
            },
            asynchronous=True,
        )
        return _response(202, {"approval_id": token, "status": "STARTING"})

    if route == "GET /approvals":
        rows = [
            row
            for row in common.list_by_status("PENDING", limit=50)
            if row.get("lane", "approval") == "approval"
        ]
        return _response(200, {"approvals": [common.public_view(row) for row in rows]})

    if route == "GET /approvals/{id}":
        item = common.get(params.get("id", ""))
        if item is None or item.get("lane", "approval") != "approval":
            return _response(404, {"error": "unknown approval"})
        return _response(200, common.public_view(item))

    if route == "POST /approvals/{id}/decision":
        approval_id = params.get("id", "")
        body = _parse_body(event)
        if body is None:
            return _response(400, {"error": "request body must be a JSON object"})
        decision = body.get("decision")
        if decision not in {"approve", "decline"}:
            return _response(400, {"error": "decision must be approve or decline"})
        item = common.get(approval_id)
        if item is None or item.get("lane", "approval") != "approval":
            return _response(404, {"error": "unknown approval"})
        import time as _time

        if int(item.get("expires_ts", 0)) and int(item["expires_ts"]) < int(_time.time()):
            return _response(409, {"error": "approval has expired"})
        target = "APPROVED" if decision == "approve" else "DECLINED"
        claims = _claims(event)
        moved = common.transition(
            approval_id,
            "PENDING",
            target,
            decided_by=claims.get("sub", ""),
            decided_by_name=claims.get("username", claims.get("cognito:username", "")),
            decided_at=common.now_iso(),
        )
        if moved is None:
            return _response(409, {"error": "approval is no longer pending"})
        _invoke_runner({"action": "resume", "approval_id": approval_id}, asynchronous=True)
        return _response(202, {"approval_id": approval_id, "status": target})

    return _response(404, {"error": "unknown route"})

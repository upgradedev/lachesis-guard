"""Shared approval-store logic for the Lachesis Approvals Lambdas.

One DynamoDB table, one row per approval:

    approval_id   (PK)  unguessable id, also the /try token
    session_id          AgentCore runtime session (33+ chars)
    status              PENDING | APPROVED | DECLINED | RESUMING | DONE |
                        EXPIRED | REFUSED_DONE
    card                {tool, reason, created_at, expires_at}
    interrupts          [{interruptId, name, reason}]
    decided_by          Cognito sub of the approver (never an email)
    decided_by_name     Cognito username
    result_text         agent's final text after a resume
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
import secrets
import time

import boto3
from botocore.exceptions import ClientError

TABLE = os.environ.get("APPROVALS_TABLE", "lachesis-approvals")
RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
REGION = os.environ.get("AWS_REGION", "eu-west-1")
TTL_SECONDS = int(os.environ.get("APPROVAL_TTL_SECONDS", "1800"))
TRY_DAILY_CAP = int(os.environ.get("TRY_DAILY_CAP", "25"))

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


def invoke_agent(session_id: str, payload: dict) -> dict:
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    response = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
    )
    return json.loads(response["response"].read())


def public_view(item: dict) -> dict:
    """What the console and /try may see: no session internals."""
    return {
        "approval_id": item["approval_id"],
        "status": item["status"],
        "lane": item.get("lane", "approval"),
        "card": json.loads(item.get("card") or "{}"),
        "decided_by_name": item.get("decided_by_name", ""),
        "result_text": item.get("result_text", ""),
        "created_ts": int(item.get("created_ts", 0)),
    }

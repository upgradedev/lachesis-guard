"""The Approvals HTTP API Lambda (B1) plus the anonymous /try lane (B2).

Routes (HTTP API, see infra/stacks/approvals.py):
- GET  /approvals            approvers group only: pending approver-lane rows
- GET  /approvals/{id}       approvers group only
- POST /approvals/{id}/decision  approvers group only; conditional write,
                             409 on a repeat, 202 + async runner resume
- POST /try                  no auth: starts the canned demo run, returns
                             the unguessable approval id as its token
- GET  /try/{token}          no auth: that run's public view only
- GET  /try/{token}/events   no auth: that run's ledger (B5), try lane only
- GET  /approvals/{id}/events  approvers group only: an approval-lane
                             run's ledger (B5)

A ledger is the run's rows of lachesis-run-events cut to the whitelist
of ledger.py (seq, type, at, status, and the run and triage fields the
publisher relays), never arguments, prompts, tokens, addresses or the
Cognito sub. Each ledger route checks the lane on the run row first, so
a /try token never reads an approval-lane run and the reverse.

Authorization is checked here from the JWT claims the HTTP API's JWT
authorizer already validated: membership in the `approvers` group.
Anything else is 403. The approver is recorded by Cognito `sub` and
username; an email never enters the record (the token carries none).

This module is a thin adapter: every state change is a command of
approval/service.py (one transaction each). A start writes the STARTING
row, and on /try both daily counters, before it answers 202, so the
token a caller polls always exists.

Every route answers with a mapped status, never an unhandled exception:
a start that loses to concurrent transactions (the shared /try counter)
is 503 "busy" and consumes nothing; a failed hand-off of a start ends
the run ERROR and answers 502; a failed hand-off of a decision still
answers 202, because the decision is committed and the sweeper
re-dispatches the resume of a decided run (runner.sweep).
"""

from __future__ import annotations

import contextlib
import json
import os
import re

import boto3
import common
import ledger
from approval import domain, service

RUNNER_ARN = os.environ.get("RUNNER_ARN", "")
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


# The shape of an approval id or /try token: common.new_approval_id makes
# "appr-" plus 32 URL-safe characters. The check is looser on length so
# test ids stay valid; it exists to refuse, before any read, a key that is
# too long for DynamoDB (a ValidationException, so a 500) and the keys of
# the counter rows (try-ip-..., try-counter-...).
_ID_SHAPE = re.compile(r"appr-[A-Za-z0-9_-]{1,64}")


def _valid_id(value: str) -> bool:
    return bool(_ID_SHAPE.fullmatch(value or ""))


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


def _error(exc: domain.DomainError, not_found: str = "") -> dict:
    """A refused command as its HTTP answer; the message is public text."""
    if isinstance(exc, domain.NotFound) and not_found:
        return _response(exc.http_status, {"error": not_found})
    return _response(exc.http_status, {"error": str(exc)})


def _dispatch_start(run: domain.Run, prompt: str, id_key: str) -> dict:
    """Hand a committed STARTING run to the runner. If the hand-off
    itself fails, the run ends ERROR at once instead of sitting in
    STARTING until the sweeper closes it."""
    try:
        _invoke_runner(
            {"action": "start", "prompt": prompt, "approval_id": run.approval_id},
            asynchronous=True,
        )
    except Exception as exc:
        # Whatever this write raises (a lost race, throttling, contention),
        # the caller still gets the 502; the sweeper closes a run left in
        # STARTING.
        with contextlib.suppress(Exception):
            service.record_failure(run, common.error_text(exc))
        return _response(502, {"error": "the run could not be handed to the runner"})
    return _response(202, {id_key: run.approval_id, "status": run.status})


def _dispatch_resume(approval_id: str) -> None:
    """Hand a committed decision to the runner. A failure is not the
    caller's: the decision is recorded, and the sweeper re-dispatches the
    resume of a run that stays APPROVED or DECLINED (runner.sweep)."""
    try:
        _invoke_runner({"action": "resume", "approval_id": approval_id}, asynchronous=True)
    except Exception as exc:
        print(json.dumps({"resume_hand_off_failed": approval_id, "error": type(exc).__name__}))


def _ledger(approval_id: str) -> dict:
    """The run's ledger. A failed read is 503, never an unhandled error;
    only the exception type name is logged."""
    try:
        events = ledger.read(approval_id)
    except Exception as exc:
        print(json.dumps({"ledger_read_failed": type(exc).__name__}))
        return _response(503, {"error": "the run ledger could not be read"})
    return _response(200, {"events": events})


def _decision(event: dict) -> tuple[str | None, dict | None]:
    """The decision in the body, or an error response."""
    body = _parse_body(event)
    if body is None:
        return None, _response(400, {"error": "request body must be a JSON object"})
    decision = body.get("decision")
    if decision not in {"approve", "decline"}:
        return None, _response(400, {"error": "decision must be approve or decline"})
    return decision, None


def handler(event, _context):
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}

    if route == "POST /try":
        source_ip = (
            event.get("requestContext", {}).get("http", {}).get("sourceIp", "unknown")
        )
        try:
            run = service.start_try(source_ip)
        except domain.CapReached as exc:
            return _error(exc)
        except domain.Conflict:
            # On a start only contention gets here (the run row and its
            # event have fresh keys): a retryable 503, nothing consumed.
            return _error(domain.Busy())
        return _dispatch_start(run, CANNED_PROMPT, "token")

    if route == "GET /try/{token}":
        token = params.get("token", "")
        if not _valid_id(token):
            return _response(404, {"error": "unknown token"})
        item = common.get(token)
        if item is None or item.get("lane") != "try":
            return _response(404, {"error": "unknown token"})
        return _response(200, common.public_view(item))

    if route == "GET /try/{token}/events":
        token = params.get("token", "")
        if not _valid_id(token):
            return _response(404, {"error": "unknown token"})
        item = common.get(token)
        if item is None or item.get("lane") != "try":
            return _response(404, {"error": "unknown token"})
        return _ledger(token)

    if route == "POST /try/{token}/decision":
        token = params.get("token", "")
        if not _valid_id(token):
            return _response(404, {"error": "unknown token"})
        decision, refused = _decision(event)
        if refused:
            return refused
        try:
            run = service.record_decision(token, "try", decision, "try-token", "try-visitor")
        except domain.DomainError as exc:
            return _error(exc, "unknown token")
        _dispatch_resume(token)
        return _response(202, {"token": token, "status": run.status})

    # Everything below needs an approver.
    if not _is_approver(event):
        return _response(403, {"error": "approvers group membership required"})

    if route == "POST /approvals/start":
        body = _parse_body(event)
        if body is None:
            return _response(400, {"error": "request body must be a JSON object"})
        try:
            run = service.start_run("approval")
        except domain.Conflict:
            return _error(domain.Busy())
        return _dispatch_start(run, str(body.get("prompt") or CANNED_PROMPT), "approval_id")

    if route == "GET /approvals":
        rows = [
            row
            for row in common.list_by_status("PENDING", limit=50)
            if row.get("lane", "approval") == "approval"
        ]
        return _response(200, {"approvals": [common.public_view(row) for row in rows]})

    if route == "GET /approvals/{id}":
        approval_id = params.get("id", "")
        item = common.get(approval_id) if _valid_id(approval_id) else None
        if item is None or item.get("lane", "approval") != "approval":
            return _response(404, {"error": "unknown approval"})
        return _response(200, common.public_view(item))

    if route == "GET /approvals/{id}/events":
        approval_id = params.get("id", "")
        item = common.get(approval_id) if _valid_id(approval_id) else None
        if item is None or item.get("lane", "approval") != "approval":
            return _response(404, {"error": "unknown approval"})
        return _ledger(approval_id)

    if route == "POST /approvals/{id}/decision":
        approval_id = params.get("id", "")
        if not _valid_id(approval_id):
            return _response(404, {"error": "unknown approval"})
        decision, refused = _decision(event)
        if refused:
            return refused
        claims = _claims(event)
        try:
            run = service.record_decision(
                approval_id,
                "approval",
                decision,
                claims.get("sub", ""),
                claims.get("username", claims.get("cognito:username", "")),
            )
        except domain.DomainError as exc:
            return _error(exc, "unknown approval")
        _dispatch_resume(approval_id)
        return _response(202, {"approval_id": approval_id, "status": run.status})

    return _response(404, {"error": "unknown route"})

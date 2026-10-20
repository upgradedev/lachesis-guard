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
- GET  /projects             no auth: the registered projects with their
                             counts (B7a, activity/read.py)
- GET  /projects/{key}/activity  no auth: a project's recent commits,
                             public repositories only
- GET  /approvals/projects   approvers group only: the same, every
                             repository in full
- GET  /approvals/projects/{key}/activity  approvers group only

B7b, the merge gate: POST /gate/pulls and GET /gate/{id} are served by
gate_api.py behind the GitHub Actions OIDC authorizer, not here. A gate
run is decided here, on the approver routes: GET /approvals lists its
PENDING rows beside the approval lane's, GET /approvals/{id} and its
/events read it, and POST /approvals/{id}/decision records the decision
and dispatches no resume, because a gate run has no agent behind it.
The /try routes never read a gate run (their lane check is "try").

A route with no authorizer never carries JWT claims, so each project
read has two routes: the anonymous one redacts private repositories
(activity/read.py), the approver one is behind the JWT authorizer. A
project key is checked against the committed registry before any read.

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

B6, behind USE_STEP_FUNCTIONS (default "false"; orchestrator.py): with
the flag on, a start writes the same STARTING row, marked as owned by
the lachesis-run workflow and carrying its prompt, and starts the
execution instead of invoking the runner. A decision on a run the
workflow owns is checked against the same refusals without a write, then
sent with SendTaskSuccess (a wait that already ended is 409, any other
failure 503); the workflow records it. The one status this API still
writes for such a run is ERROR when StartExecution fails, because then
no execution exists to write it; if an execution did start and has
already taken the run, that write conflicts and the start answers 202.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time

import boto3
import common
import ledger
import orchestrator
from activity import read as activity_read
from activity import registry as activity_registry
from activity import store as activity_store
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

# The longest prompt a workflow-owned run stores on its row (B6). The
# runner path sends the prompt in its async event instead.
WORKFLOW_PROMPT_MAX_CHARS = 16000


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


def _claimed_by_an_execution(approval_id: str) -> domain.Run | None:
    """The row, if an execution has taken it: claimed, or moved on from
    STARTING. None when it is still an unclaimed STARTING row, or when it
    cannot be read."""
    try:
        current = service.unit_of_work().get(approval_id)
    except Exception:
        return None
    if current is None:
        return None
    if current.status == domain.STARTING and not current.row.get("claimed_request_id"):
        return None
    return current


def _start_workflow(run: domain.Run, id_key: str) -> dict:
    """Start the lachesis-run execution of a committed STARTING run (B6).
    If StartExecution fails, the run ends ERROR at once, as a failed
    runner hand-off does.

    StartExecution can succeed on the server while every client attempt
    fails (a read timeout after the request was accepted). The execution
    then claims the run, and the ERROR write conflicts. A row that an
    execution has taken is answered 202 with its real status, since the
    run goes on; 502 means no execution has it. An unclaimed STARTING row
    whose ERROR write failed is still 502, and the sweeper closes it."""
    try:
        orchestrator.start(run.approval_id)
    except Exception as exc:
        print(json.dumps({"workflow_start_failed": type(exc).__name__}))
        try:
            service.record_failure(run, common.error_text(exc))
        except domain.Conflict:
            owned = _claimed_by_an_execution(run.approval_id)
            if owned is not None:
                return _response(202, {id_key: owned.approval_id, "status": owned.status})
        except Exception:  # the ERROR write failed: the sweeper closes STARTING
            pass
        return _response(502, {"error": "the run could not be handed to the workflow"})
    return _response(202, {id_key: run.approval_id, "status": run.status})


def _decide_through_workflow(
    owned: domain.WorkflowOwned, decision: str, decided_by: str, decided_by_name: str, id_key: str
) -> dict:
    """A decision on a run the workflow owns (B6). The refusals of
    service.record_decision were already checked without a write; the
    task token must be on the row, then one SendTaskSuccess hands the
    decision to the workflow, which records it. The answer carries the
    row's real status, still PENDING: this API wrote nothing."""
    run = owned.run
    if not run.task_token:
        return _error(domain.NotDecidable())
    try:
        orchestrator.send_decision(
            run.task_token, decision, decided_by, decided_by_name, owned.now
        )
    except orchestrator.WaitOver:
        return _error(domain.Conflict())
    except Exception as exc:
        print(json.dumps({"decision_hand_off_failed": type(exc).__name__}))
        return _error(domain.Busy())
    return _response(202, {id_key: run.approval_id, "status": run.status})


def _dispatch_start(run: domain.Run, prompt: str, id_key: str) -> dict:
    """Hand a committed STARTING run to the runner, or start its
    workflow when the workflow owns it (B6). If the hand-off itself
    fails, the run ends ERROR at once instead of sitting in STARTING
    until the sweeper closes it."""
    if run.workflow_owned:
        return _start_workflow(run, id_key)
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


# The lanes the approver routes read and decide (B7b adds the gate).
APPROVER_LANES = ("approval", domain.GATE)


def _ledger(approval_id: str, lane: str | tuple[str, ...], not_found: str) -> dict:
    """The run's ledger, after the lane check on the run row: an invalid
    id, an unknown run or a run of the other lane is 404 and the event log
    is never read. A failed read, of the run row or of the log, is 503,
    never an unhandled error; only the exception type name is logged."""
    if not _valid_id(approval_id):
        return _response(404, {"error": not_found})
    try:
        item = common.get(approval_id)
        # A row without a lane predates /try and is an approval-lane run.
        lanes = (lane,) if isinstance(lane, str) else lane
        if item is None or item.get("lane", "approval") not in lanes:
            return _response(404, {"error": not_found})
        events = ledger.read(approval_id)
    except Exception as exc:
        print(json.dumps({"ledger_read_failed": type(exc).__name__}))
        return _response(503, {"error": "the run ledger could not be read"})
    return _response(200, {"events": events})


def _activity_store() -> activity_store.ActivityStore:
    return activity_store.ActivityStore()


def _projects(approver: bool) -> dict:
    """GET /projects (anonymous) or GET /approvals/projects. A failed read
    is 503; only the exception type name is logged."""
    try:
        view = activity_read.projects_view(
            activity_registry.load(), _activity_store(), approver
        )
    except Exception as exc:
        print(json.dumps({"projects_read_failed": type(exc).__name__}))
        return _response(503, {"error": "the project activity could not be read"})
    return _response(200, view)


def _project_activity(key: str, approver: bool) -> dict:
    """A project's recent commits. An unknown key is 404 before any read."""
    project = (
        activity_registry.find(activity_registry.load(), key)
        if activity_registry.valid_key(key)
        else None
    )
    if project is None:
        return _response(404, {"error": "unknown project"})
    try:
        view = activity_read.activity_view(project, _activity_store(), approver, time.time())
    except Exception as exc:
        print(json.dumps({"activity_read_failed": type(exc).__name__}))
        return _response(503, {"error": "the project activity could not be read"})
    return _response(200, view)


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
        workflow = (
            {"orchestrator": domain.WORKFLOW, "prompt": CANNED_PROMPT}
            if orchestrator.enabled()
            else {}
        )
        try:
            run = service.start_try(source_ip, **workflow)
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
        return _ledger(params.get("token", ""), "try", "unknown token")

    if route == "GET /projects":
        return _projects(approver=False)

    if route == "GET /projects/{key}/activity":
        return _project_activity(params.get("key", ""), approver=False)

    if route == "POST /try/{token}/decision":
        token = params.get("token", "")
        if not _valid_id(token):
            return _response(404, {"error": "unknown token"})
        decision, refused = _decision(event)
        if refused:
            return refused
        try:
            run = service.record_decision(token, "try", decision, "try-token", "try-visitor")
        except domain.WorkflowOwned as owned:
            return _decide_through_workflow(owned, decision, "try-token", "try-visitor", "token")
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
        prompt = str(body.get("prompt") or CANNED_PROMPT)
        workflow = {}
        if orchestrator.enabled():
            if len(prompt) > WORKFLOW_PROMPT_MAX_CHARS:
                return _response(400, {"error": "prompt is too long"})
            workflow = {"orchestrator": domain.WORKFLOW, "prompt": prompt}
        try:
            run = service.start_run("approval", **workflow)
        except domain.Conflict:
            return _error(domain.Busy())
        return _dispatch_start(run, prompt, "approval_id")

    if route == "GET /approvals":
        rows = common.list_by_status_in_lanes("PENDING", APPROVER_LANES, limit=50)
        return _response(200, {"approvals": [common.public_view(row) for row in rows]})

    if route == "GET /approvals/projects":
        return _projects(approver=True)

    if route == "GET /approvals/projects/{key}/activity":
        return _project_activity(params.get("key", ""), approver=True)

    if route == "GET /approvals/{id}":
        approval_id = params.get("id", "")
        item = common.get(approval_id) if _valid_id(approval_id) else None
        if item is None or item.get("lane", "approval") not in APPROVER_LANES:
            return _response(404, {"error": "unknown approval"})
        return _response(200, common.public_view(item))

    if route == "GET /approvals/{id}/events":
        return _ledger(params.get("id", ""), APPROVER_LANES, "unknown approval")

    if route == "POST /approvals/{id}/decision":
        approval_id = params.get("id", "")
        if not _valid_id(approval_id):
            return _response(404, {"error": "unknown approval"})
        decision, refused = _decision(event)
        if refused:
            return refused
        claims = _claims(event)
        decided_by = claims.get("sub", "")
        decided_by_name = claims.get("username", claims.get("cognito:username", ""))
        try:
            run = service.record_decision(
                approval_id, APPROVER_LANES, decision, decided_by, decided_by_name
            )
        except domain.WorkflowOwned as owned:
            return _decide_through_workflow(
                owned, decision, decided_by, decided_by_name, "approval_id"
            )
        except domain.DomainError as exc:
            return _error(exc, "unknown approval")
        # A gate run's decision is its end: there is no agent to resume.
        if not run.is_gate:
            _dispatch_resume(approval_id)
        return _response(202, {"approval_id": approval_id, "status": run.status})

    return _response(404, {"error": "unknown route"})

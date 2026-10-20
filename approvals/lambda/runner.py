"""The runner Lambda: the only principal allowed to invoke the agent.

Two actions:

- ``start``: claim the STARTING run the API already wrote (a duplicate
  async delivery finds it claimed and does nothing), then invoke the
  agent with a prompt. If the agent completes, the final text is
  returned. If Lachesis pauses it (interrupt), the run moves to
  PENDING and the paused call appears in the
  Approvals console queue, where the approver sees and decides it. An
  SES card is optional and is sent only when LACHESIS_SES_FROM and
  LACHESIS_SES_TO are set; the hosted demo leaves them unset. GET never
  approves.
- ``resume``: move an APPROVED/DECLINED row to RESUMING (conditional:
  a duplicate resume event finds nothing to move and stops), resume
  the agent session with the human's answer, and store the outcome.
  Invoked with MaximumRetryAttempts=0 so retries never double-resume.

``sweep`` is not an action of this handler: the sweeper Lambda runs it
in-process under its own role (sweeper.py). It resumes expired PENDING
rows with "no" and marks them EXPIRED, so a forgotten pause ends
refused, never open, and it re-dispatches the resume of a decided run
whose hand-off from the API was lost.

Every agent answer goes through contract.read_result: an agent error
(which comes back with HTTP 200) is recorded as ERROR, never as DONE.
A failed resume returns to PENDING, where a person could decide again,
only when common.failed_before_send proves the agent was never invoked;
any other failure may already have run the paused call and ends ERROR.
A start is retried twice, with a short backoff, only for those pre-send
failures and for a throttling or quota refusal (START_RETRY_CODES).

Every state write is a command of approval/service.py (one transaction,
with its domain events). A command that loses a race (the sweeper closed
the run first, for example) raises domain.Conflict; the runner then
reports "conflict" and writes nothing.
"""

from __future__ import annotations

import datetime
import html
import json
import os
import time
import uuid

import boto3
import common
import contract
from approval import domain, service
from botocore.exceptions import ClientError

SES_FROM = os.environ.get("SES_FROM", "")
SES_TO = os.environ.get("SES_TO", "")
CONSOLE_URL = os.environ.get("CONSOLE_URL", "")


def _send_card(item: dict) -> None:
    if not SES_FROM or not SES_TO:
        return
    card = json.loads(item.get("card") or "{}")
    reason = str(card.get("reason", ""))
    safe_reason = html.escape(reason)
    safe_id = html.escape(str(item.get("approval_id", "")))
    safe_expires = html.escape(str(card.get("expires_at", "")))
    safe_url = html.escape(CONSOLE_URL, quote=True)
    body_html = (
        "<h2>Lachesis needs a person</h2>"
        f"<p>{safe_reason}</p>"
        f"<p>Approval <code>{safe_id}</code> expires at "
        f"{safe_expires} UTC.</p>"
        f'<p><a href="{safe_url}">Open the Lachesis Approvals console</a> '
        "to approve or decline. Opening this mail decides nothing.</p>"
    )
    boto3.client("ses", region_name=common.REGION).send_email(
        Source=SES_FROM,
        Destination={"ToAddresses": [SES_TO]},
        Message={
            "Subject": {"Data": "Lachesis: a tool call is paused for you"},
            "Body": {"Html": {"Data": body_html}, "Text": {"Data": reason}},
        },
    )


def _pending_card(result: dict) -> dict:
    """The approval card for a pause: the interrupt reason plus the
    pausing triage row's Decider fields (risk, confidence, latency,
    layer) and any prior proceed steps in the same turn, so the console
    can show what Decider measured (F2). Used for the first pause and
    for every re-pause after a resume; the resume path used to rewrite
    the card with the reason only, which silently dropped the Decider
    fields (found live, 2026-10-15)."""
    import datetime

    interrupts = result.get("interrupts", [])
    triage = result.get("triage", [])
    pausing = next(
        (row for row in reversed(triage) if row.get("verdict") in {"confirm", "deny"}),
        triage[-1] if triage else {},
    )
    prior_steps = [
        {
            "tool": row.get("tool", ""),
            "verdict": row.get("verdict", ""),
            "risk": row.get("risk", ""),
            "confidence": row.get("confidence"),
            "grounded": row.get("grounded"),
            "latency_ms": row.get("latency_ms"),
            "layer": row.get("layer", ""),
        }
        for row in triage
        if row is not pausing and row.get("verdict") == "proceed"
    ]
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=common.TTL_SECONDS
    )
    return {
        "reason": interrupts[0].get("reason", "") if interrupts else "",
        "tool": pausing.get("tool", ""),
        "risk": pausing.get("risk", ""),
        "confidence": pausing.get("confidence"),
        "latency_ms": pausing.get("latency_ms"),
        "layer": pausing.get("layer", ""),
        "prior_steps": prior_steps,
        "changed_files": list(result.get("changed_files") or []),
        "created_at": common.now_iso(),
        "expires_at": expires.isoformat(timespec="seconds"),
    }


# Margin left after an agent call for the outcome write, and the least
# Lambda time the sweeper needs before it starts another expiry resume.
OUTCOME_MARGIN_S = 20
SWEEP_MIN_BUDGET_S = 60

# A start is retried only when the request provably did not run the
# agent: it failed before it was sent, or the service refused it with
# one of these codes. A resume is never retried here (see resume).
START_RETRY_CODES = frozenset({"ThrottlingException", "ServiceQuotaExceededException"})
START_BACKOFF_S = (1.0, 2.0)
_sleep = time.sleep


def _remaining_s(context) -> float | None:
    """Seconds left in this Lambda invocation, or None without a context."""
    get = getattr(context, "get_remaining_time_in_millis", None)
    return get() / 1000.0 if callable(get) else None


def _read_timeout(context) -> float:
    """The agent read timeout: the remaining Lambda time minus the margin
    for the outcome write, capped at common.AGENT_READ_TIMEOUT, so a slow
    run ends in a recorded ReadTimeoutError and not in a Lambda timeout
    that leaves the row behind."""
    remaining = _remaining_s(context)
    if remaining is None:
        return float(common.AGENT_READ_TIMEOUT)
    return max(5.0, min(float(common.AGENT_READ_TIMEOUT), remaining - OUTCOME_MARGIN_S))


def _start_retryable(exc: BaseException) -> bool:
    if common.failed_before_send(exc):
        return True
    # isinstance, not an exact type: botocore raises modeled errors such
    # as ThrottlingException as ClientError subclasses. Any ClientError
    # carries a service response, so only its code decides.
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code") in START_RETRY_CODES
    return False


def _invoke_start(session_id: str, prompt: str, context) -> dict:
    for backoff in (*START_BACKOFF_S, None):
        try:
            return common.invoke_agent(
                session_id,
                {"prompt": prompt, "session_id": session_id},
                read_timeout=_read_timeout(context),
            )
        except Exception as exc:
            if backoff is None or not _start_retryable(exc):
                raise
            _sleep(backoff)
    raise AssertionError("unreachable")  # pragma: no cover


def _request_id(context) -> str:
    """The Lambda request id that claims a run; a fresh id without one."""
    return str(getattr(context, "aws_request_id", "") or "local-" + uuid.uuid4().hex)


def _pause(run: domain.Run, result: contract.AgentResult) -> domain.Run:
    updated = service.record_pause(run, result.interrupts, _pending_card(result.as_payload()))
    if updated.lane == "approval":
        _send_card(updated.row)
    return updated


def start(prompt: str, approval_id: str, context=None) -> dict:
    run = service.claim_run(approval_id, _request_id(context))
    if run is None:
        return {"status": "noop", "approval_id": approval_id}
    try:
        try:
            raw = _invoke_start(run.session_id, prompt, context)
        except Exception as exc:
            service.record_failure(run, common.error_text(exc))
            return {"status": "error", "approval_id": approval_id}
        result = contract.read_result(raw)
        if result.is_error:
            service.record_failure(run, result.error)
            return {"status": "error", "approval_id": approval_id}
        if result.kind == contract.COMPLETED:
            service.record_completion(run, result.text)
            return {"status": "completed", "approval_id": approval_id}
        _pause(run, result)
        return {"status": "interrupt", "approval_id": approval_id}
    except domain.Conflict:
        return {"status": "conflict", "approval_id": approval_id}


def _answers(run: domain.Run, answer: str) -> list[dict]:
    return [{"interruptId": item["interruptId"], "response": answer} for item in run.interrupts]


def resume(approval_id: str, context=None) -> dict:
    try:
        run, answer = service.resume(approval_id)
    except domain.NotFound:
        return {"status": "missing"}
    except domain.InvalidTransition as exc:
        return {"status": "noop", "current": exc.current}
    except domain.Conflict:
        return {"status": "noop", "current": "moved-by-another"}
    try:
        try:
            raw = common.invoke_agent(
                run.session_id,
                {"session_id": run.session_id, "interrupt_responses": _answers(run, answer)},
                read_timeout=_read_timeout(context),
            )
        except Exception as exc:  # never strand a row in RESUMING
            if common.failed_before_send(exc):
                # The agent was never invoked: the decision can be taken again.
                return {"status": "error-resumable", "current": service.resume_not_sent(run)}
            # The request may have reached the agent, so the paused call may
            # already have run: nobody may be asked to decide it a second time.
            service.record_failure(run, common.error_text(exc))
            return {"status": "ERROR", "error": type(exc).__name__}
        result = contract.read_result(raw)
        if result.kind == contract.INTERRUPT:
            # The resumed run paused again: a fresh pending state on the
            # same approval id, with a renewed expiry window. The decision
            # just consumed joins the history (see domain.Run.pause).
            _pause(run, result)
            return {"status": "interrupt-again", "approval_id": approval_id}
        if result.is_error:
            service.record_failure(run, result.error)
            return {"status": "ERROR", "text": ""}
        return {"status": service.record_completion(run, result.text), "text": result.text}
    except domain.Conflict:
        return {"status": "conflict", "approval_id": approval_id}


# A claimed start can sit in STARTING for up to the runner's read
# timeout (AGENT_READ_TIMEOUT, under the 900 s Lambda limit) after a
# queue delay. The async queue delay is bounded by the runner's
# MaximumEventAgeInSeconds (RUNNER_MAX_EVENT_AGE_S in
# infra/stacks/approvals.py: an older event is dropped, never run), so
# 300 s of queue plus 900 s of Lambda stays under the 1260 s after which
# the sweeper closes STARTING (ESTIMATE, from the design review).
# RESUMING is closed after 1200 s.
STARTING_TIMEOUT_S = 1260
RESUMING_TIMEOUT_S = 1200
# A decided run (APPROVED or DECLINED) whose resume event has not moved
# it to RESUMING after this long is handed to the runner again: the API's
# hand-off failed, or the event aged out of the queue. A duplicate resume
# is a no-op (the APPROVED/DECLINED -> RESUMING move is conditioned on
# the version), so re-dispatching a run whose event is merely late is
# safe.
REDISPATCH_AFTER_S = 120
RUNNER_ARN = os.environ.get("RUNNER_ARN", "")


def _decided_ts(item: dict) -> int:
    """When the current decision was recorded; 0 (so: old) when unknown."""
    try:
        return int(datetime.datetime.fromisoformat(str(item.get("decided_at", ""))).timestamp())
    except (TypeError, ValueError):
        return 0


def _invoke_runner(payload: dict) -> None:
    """Async-invoke the runner Lambda (the sweeper's resume re-dispatch)."""
    boto3.client("lambda", region_name=common.REGION).invoke(
        FunctionName=RUNNER_ARN,
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )


def _quietly(command, *args) -> bool:
    """Run one command; False when the run moved first (domain.Conflict)."""
    try:
        command(*args)
    except domain.Conflict:
        return False
    return True


def sweep(context=None) -> dict:
    # Rows whose start never reported back sit in STARTING; anything
    # older than STARTING_TIMEOUT_S there is terminal.
    closed_starting = 0
    for item in common.list_by_status("STARTING", limit=50):
        if int(time.time()) - int(item.get("created_ts", 0)) < STARTING_TIMEOUT_S:
            continue
        run = domain.Run.from_item(item)
        if _quietly(service.record_failure, run, "StartTimeout: the start did not report back"):
            closed_starting += 1

    # A resume whose Lambda died mid-invoke leaves RESUMING behind. Nothing
    # proves the agent was not invoked, so the paused call may already
    # have run: after twenty minutes the row ends ERROR, never PENDING,
    # where a person could decide it a second time.
    rescued = 0
    for item in common.list_by_status("RESUMING", limit=50):
        if int(time.time()) - int(item.get("resuming_ts", 0)) < RESUMING_TIMEOUT_S:
            continue
        run = domain.Run.from_item(item)
        if _quietly(
            service.record_failure, run, "ResumeOutcomeUnknown: the resume did not report back"
        ):
            rescued += 1

    # A decision whose resume never reached the runner leaves the run
    # APPROVED or DECLINED, which nothing else moves: hand it over again.
    redispatched = 0
    for status in (domain.APPROVED, domain.DECLINED):
        for item in common.list_by_status(status, limit=50):
            if int(time.time()) - _decided_ts(item) < REDISPATCH_AFTER_S:
                continue
            try:
                _invoke_runner({"action": "resume", "approval_id": str(item["approval_id"])})
            except Exception:  # never crash the sweep; the next one tries again
                continue
            redispatched += 1

    expired = [
        item
        for item in common.list_by_status("PENDING", limit=100)
        if int(item.get("expires_ts", 0)) < int(time.time())
    ]
    closed_try = 0
    deferred = 0
    for item in expired:
        run = domain.Run.from_item(item)
        if run.lane == "try":
            # A public /try pause that nobody decided simply ends. It
            # must not resume the agent: resuming re-invokes the runtime
            # and wakes the Decider GPU a second time for a run nobody
            # is watching (measured 2026-10-15: expiries at 21:54 and
            # 22:09 UTC each re-woke it).
            if _quietly(service.expire, run):
                closed_try += 1
            continue
        remaining = _remaining_s(context)
        if remaining is not None and remaining < SWEEP_MIN_BUDGET_S:
            # Not enough Lambda time for another resume: the row stays
            # PENDING and the next sweep resumes it.
            deferred += 1
            continue
        try:
            answer = service.expiry_resume(run)
        except domain.Conflict:
            continue
        try:
            raw = common.invoke_agent(
                run.session_id,
                {"session_id": run.session_id, "interrupt_responses": _answers(run, answer)},
                read_timeout=_read_timeout(context),
            )
        except Exception as exc:  # never crash the sweep
            if common.failed_before_send(exc):
                _quietly(service.resume_not_sent, run)  # never sent: retried next sweep
            else:
                _quietly(service.record_failure, run, common.error_text(exc))
            continue
        outcome = contract.read_result(raw)
        if outcome.is_error:
            _quietly(service.record_failure, run, outcome.error)
            continue
        _quietly(service.expire, run)
    return {
        "expired": len(expired),
        "rescued": rescued,
        "closed_starting": closed_starting,
        "closed_try": closed_try,
        "deferred": deferred,
        "redispatched": redispatched,
    }


def handler(event, context):
    action = event.get("action")
    if action == "start":
        return start(
            str(event.get("prompt", "")),
            str(event.get("approval_id", "")),
            context=context,
        )
    if action == "resume":
        return resume(str(event.get("approval_id", "")), context=context)
    return {"status": "error", "error": f"unknown action {action!r}"}

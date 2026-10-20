"""The runner Lambda: the only principal allowed to invoke the agent.

Two actions:

- ``start``: invoke the agent with a prompt. If the agent completes,
  the final text is returned. If Lachesis pauses it (interrupt), a
  PENDING approval row is written and the paused call appears in the
  Approvals console queue, where the approver sees and decides it. An
  SES card is optional and is sent only when LACHESIS_SES_FROM and
  LACHESIS_SES_TO are set; the hosted demo leaves them unset. GET never
  approves.
- ``resume``: move an APPROVED/DECLINED row to RESUMING (conditional:
  a duplicate resume event finds nothing to move and stops), resume
  the agent session with the human's answer, and store the outcome.
  Invoked with MaximumRetryAttempts=0 so retries never double-resume.

Also ``sweep``: expired PENDING rows are resumed with "no" and marked
EXPIRED, so a forgotten pause ends refused, never open.

Every agent answer goes through contract.read_result: an agent error
(which comes back with HTTP 200) is recorded as ERROR, never as DONE.
A failed resume returns to PENDING, where a person could decide again,
only when common.failed_before_send proves the agent was never invoked;
any other failure may already have run the paused call and ends ERROR.
A start is retried twice, with a short backoff, only for those pre-send
failures and for a throttling or quota refusal (START_RETRY_CODES).
"""

from __future__ import annotations

import html
import json
import os
import time

import boto3
import common
import contract
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


def start(prompt: str, lane: str, approval_id: str = "", context=None) -> dict:
    approval_id = approval_id or common.new_approval_id()
    session_id = common.new_session_id()
    common.create_row(approval_id, session_id, lane)
    try:
        raw = _invoke_start(session_id, prompt, context)
    except Exception as exc:
        common.transition(approval_id, "STARTING", "ERROR", result_text=common.error_text(exc))
        return {"status": "error", "approval_id": approval_id}
    result = contract.read_result(raw)
    if result.is_error:
        common.transition(approval_id, "STARTING", "ERROR", result_text=result.error)
        return {"status": "error", "approval_id": approval_id}
    if result.kind == contract.COMPLETED:
        common.transition(approval_id, "STARTING", "DONE", result_text=result.text)
        return {"status": "completed", "approval_id": approval_id}
    card = _pending_card(result.as_payload())
    item = common.transition(
        approval_id,
        "STARTING",
        "PENDING",
        interrupts=json.dumps(result.interrupts),
        card=json.dumps(card),
    )
    if lane == "approval" and item is not None:
        _send_card(item)
    return {"status": "interrupt", "approval_id": approval_id}


def _decision_entry(row: dict, status: str, suffix: str = "") -> dict:
    """One history entry for the decision a resume consumed. The Cognito
    sub is not copied: the history is public, the sub is not. ``suffix``
    "_not_sent" marks a decision whose resume failed before the agent was
    invoked (see _attempt_failed)."""
    return {
        "decision": ("approve" if status == "APPROVED" else "decline") + suffix,
        "decided_by_name": str(row.get("decided_by_name", "")),
        "at": str(row.get("decided_at") or common.now_iso()),
    }


def resume(approval_id: str, context=None) -> dict:
    row = common.get(approval_id)
    if row is None:
        return {"status": "missing"}
    status = row["status"]
    if status not in {"APPROVED", "DECLINED"}:
        return {"status": "noop", "current": status}
    moved = common.transition(approval_id, status, "RESUMING", resuming_ts=int(time.time()))
    if moved is None:
        return {"status": "noop", "current": "moved-by-another"}
    answer = "y" if status == "APPROVED" else "no"
    interrupts = json.loads(row.get("interrupts") or "[]")
    try:
        raw = common.invoke_agent(
            row["session_id"],
            {
                "session_id": row["session_id"],
                "interrupt_responses": [
                    {"interruptId": item["interruptId"], "response": answer}
                    for item in interrupts
                ],
            },
            read_timeout=_read_timeout(context),
        )
    except Exception as exc:  # never strand a row in RESUMING
        if common.failed_before_send(exc):
            # The agent was never invoked: the decision can be taken again.
            return {"status": "error-resumable", "current": _attempt_failed(row, status)}
        # The request may have reached the agent, so the paused call may
        # already have run: nobody may be asked to decide it a second time.
        common.transition(approval_id, "RESUMING", "ERROR", result_text=common.error_text(exc))
        return {"status": "ERROR", "error": type(exc).__name__}
    result = contract.read_result(raw)
    if result.kind == contract.INTERRUPT:
        # The resumed run paused again: fold the new pause into a fresh
        # pending state on the same approval id, with a renewed expiry
        # window. The decision just consumed is appended to the history,
        # and decided_by / decided_by_name are kept, so a re-pause never
        # erases who decided first. The next decision overwrites them;
        # public_view shows no name while the row is PENDING or EXPIRED.
        now = int(time.time())
        history = row.get("decisions") if isinstance(row.get("decisions"), list) else []
        updated = common.transition(
            approval_id,
            "RESUMING",
            "PENDING",
            interrupts=json.dumps(result.interrupts),
            card=json.dumps(_pending_card(result.as_payload())),
            expires_ts=now + common.TTL_SECONDS,
            ttl=now + common.TTL_SECONDS + 86400,
            decisions=[*history, _decision_entry(row, status)],
        )
        if row.get("lane", "approval") == "approval" and updated is not None:
            _send_card(updated)
        return {"status": "interrupt-again", "approval_id": approval_id}
    final_status = contract.final_status(result, status)
    text = result.error if result.is_error else result.text
    common.transition(approval_id, "RESUMING", final_status, result_text=text)
    return {"status": final_status, "text": "" if result.is_error else result.text}


MAX_SWEEP_ATTEMPTS = 3


def _attempt_failed(item: dict, decided: str = "") -> str:
    """Count a resume attempt that failed before the agent was invoked
    (common.failed_before_send); the third such failure is terminal.
    Never call this for a failure that may have reached the agent.

    ``decided`` is the APPROVED or DECLINED status a person set, empty
    for a sweeper expiry. When the row returns to PENDING, that decision
    was made but never sent: it is appended to ``decisions`` as
    "<choice>_not_sent" and the current-decision fields are cleared, so
    the next decision neither overwrites it nor inherits its name."""
    attempts = int(item.get("sweep_attempts", 0)) + 1
    if attempts >= MAX_SWEEP_ATTEMPTS:
        common.transition(
            item["approval_id"], "RESUMING", "ERROR", sweep_attempts=attempts
        )
        return "ERROR"
    fields: dict = {"sweep_attempts": attempts}
    if decided:
        history = item.get("decisions") if isinstance(item.get("decisions"), list) else []
        fields.update(
            decisions=[*history, _decision_entry(item, decided, "_not_sent")],
            **NOBODY_DECIDED,
        )
    common.transition(item["approval_id"], "RESUMING", "PENDING", **fields)
    return "PENDING"


# Clears the current-decision fields when a pause ends with nobody
# deciding it. A re-pause keeps the previous pause's decider in the row;
# without this, an expiry would be credited to that person. The earlier
# decisions stay in ``decisions``.
NOBODY_DECIDED = {"decided_by": "", "decided_by_name": "", "decided_at": ""}


def sweep(context=None) -> dict:
    # Rows whose start invoke failed after create_row sit in STARTING;
    # anything older than ten minutes there is terminal.
    closed_starting = 0
    for item in common.list_by_status("STARTING", limit=50):
        if int(time.time()) - int(item.get("created_ts", 0)) < 600:
            continue
        closed = common.transition(
            item["approval_id"],
            "STARTING",
            "ERROR",
            result_text="StartTimeout: the start did not report back",
        )
        if closed is not None:
            closed_starting += 1

    # A resume whose Lambda died mid-invoke leaves RESUMING behind. Nothing
    # proves the agent was not invoked, so the paused call may already
    # have run: after twenty minutes the row ends ERROR, never PENDING,
    # where a person could decide it a second time.
    rescued = 0
    for item in common.list_by_status("RESUMING", limit=50):
        if int(time.time()) - int(item.get("resuming_ts", 0)) < 1200:
            continue
        moved = common.transition(
            item["approval_id"],
            "RESUMING",
            "ERROR",
            result_text="ResumeOutcomeUnknown: the resume did not report back",
        )
        if moved is not None:
            rescued += 1

    expired = [
        item
        for item in common.list_by_status("PENDING", limit=100)
        if int(item.get("expires_ts", 0)) < int(time.time())
    ]
    closed_try = 0
    deferred = 0
    for item in expired:
        if item.get("lane") == "try":
            # A public /try pause that nobody decided simply ends. It
            # must not resume the agent: resuming re-invokes the runtime
            # and wakes the Decider GPU a second time for a run nobody
            # is watching (measured 2026-10-15: expiries at 21:54 and
            # 22:09 UTC each re-woke it).
            if (
                common.transition(item["approval_id"], "PENDING", "EXPIRED", **NOBODY_DECIDED)
                is not None
            ):
                closed_try += 1
            continue
        remaining = _remaining_s(context)
        if remaining is not None and remaining < SWEEP_MIN_BUDGET_S:
            # Not enough Lambda time for another resume: the row stays
            # PENDING and the next sweep resumes it.
            deferred += 1
            continue
        moved = common.transition(
            item["approval_id"],
            "PENDING",
            "RESUMING",
            resuming_ts=int(time.time()),
            **NOBODY_DECIDED,
        )
        if moved is None:
            continue
        interrupts = json.loads(item.get("interrupts") or "[]")
        try:
            raw = common.invoke_agent(
                item["session_id"],
                {
                    "session_id": item["session_id"],
                    "interrupt_responses": [
                        {"interruptId": entry["interruptId"], "response": "no"}
                        for entry in interrupts
                    ],
                },
                read_timeout=_read_timeout(context),
            )
        except Exception as exc:  # never crash the sweep
            if common.failed_before_send(exc):
                _attempt_failed(item)  # never sent: retried on the next sweep
            else:
                common.transition(
                    item["approval_id"], "RESUMING", "ERROR", result_text=common.error_text(exc)
                )
            continue
        outcome = contract.read_result(raw)
        if outcome.is_error:
            common.transition(item["approval_id"], "RESUMING", "ERROR", result_text=outcome.error)
            continue
        common.transition(item["approval_id"], "RESUMING", "EXPIRED")
    return {
        "expired": len(expired),
        "rescued": rescued,
        "closed_starting": closed_starting,
        "closed_try": closed_try,
        "deferred": deferred,
    }


def handler(event, context):
    action = event.get("action")
    if action == "start":
        return start(
            str(event.get("prompt", "")),
            str(event.get("lane", "approval")),
            str(event.get("approval_id", "")),
            context=context,
        )
    if action == "resume":
        return resume(str(event.get("approval_id", "")), context=context)
    if action == "sweep":
        return sweep(context)
    return {"status": "error", "error": f"unknown action {action!r}"}

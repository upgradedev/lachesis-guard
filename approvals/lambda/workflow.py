"""The task Lambda of the lachesis-run state machine (B6).

lachesis-run is a Standard state machine (infra/stacks/run_workflow.py)
that owns one run from its start to its end when USE_STEP_FUNCTIONS is
on. It is then the single writer of the run's status: every write below
is a command of approval/service.py, one Unit of Work transaction
guarded by the status and version the run was loaded at. The state
carries the approval id and these small results only; the prompt, the
card and the interrupts stay on the run row.

One function, five actions, one per task state:

- ``turn`` (AgentTurn): a STARTING run is claimed, then the agent is
  started with the prompt on the row; a RESUMING run is claimed once
  (domain.Run.claim_resume), then resumed with the person's answer. The
  result goes through contract.read_result. An interrupt is staged on
  the row, not yet decidable, and the outcome carries the wait's bounds
  from the expiry that pause will have. A turn that finds its claim
  taken (a retried invocation) never invokes the agent: it reports the
  row's own outcome, and a claimed run with no outcome ends ERROR,
  because the agent may already have run.
- ``await`` (WaitForDecision, lambda:invoke.waitForTaskToken): the
  staged pause becomes PENDING in the same write that stores the task
  token. Any failure here is reported with SendTaskFailure on that
  token, so the workflow does not wait for a decision nobody can make.
- ``apply`` (ApplyDecision): the decision the API sent with
  SendTaskSuccess is recorded (DecisionRecorded, the token dropped),
  then the run moves to RESUMING and the workflow loops to AgentTurn. A
  decision that can no longer be applied, past the deadline or refused
  by the aggregate, sends the run to Expire, so it ends refused and
  never stays open. Contention (domain.Busy) or any other error is
  raised, and the state machine retries this idempotent task.
- ``expire`` (Expire, after the wait's timeout): as runner.sweep does,
  a /try pause simply ends EXPIRED (no agent call, no GPU wake); an
  approval-lane pause is resumed with "no", then EXPIRED.
- ``fail`` (RecordFailure, after any caught task failure): ERROR from
  STARTING or RESUMING, with only the error name. A run that is PENDING,
  APPROVED or DECLINED cannot move to ERROR by the transition table; it
  is left as it is and the execution ends Failed, which alarms.

Outcomes the Choice states branch on: interrupt, completed, refused,
failed, expired, resume.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any

import boto3
import common
import contract
import runner
from approval import domain, service

ACTIONS = ("turn", "await", "apply", "expire", "fail")

INTERRUPT = "interrupt"
COMPLETED = "completed"
REFUSED = "refused"
FAILED = "failed"
EXPIRED = "expired"
RESUME = "resume"
OUTCOMES = (INTERRUPT, COMPLETED, REFUSED, FAILED, EXPIRED, RESUME)

# No heartbeat is ever sent: a person's decision is the only completion.
# The heartbeat window therefore ends the wait at the row's expiry, and
# the task timeout, which Step Functions requires to be longer, is this
# much later. Both are caught and lead to Expire.
WAIT_GRACE_S = 5

_TERMINAL_OUTCOME = {
    domain.DONE: COMPLETED,
    domain.REFUSED_DONE: REFUSED,
    domain.EXPIRED: EXPIRED,
    domain.ERROR: FAILED,
}

# Pre-send failures of the expiry resume are tried again in this
# invocation (there is no later sweep for a workflow-owned run).
EXPIRY_BACKOFF_S = (1.0, 2.0)
_sleep = time.sleep


def _log(**fields: Any) -> None:
    print(json.dumps(fields, default=str))


def _result(outcome: str, expires_ts: int = 0, now: float = 0.0) -> dict[str, Any]:
    """Every task result has the same keys (the ResultSelector reads all
    three). The bounds are set only for an interrupt."""
    heartbeat = timeout = 0
    if outcome == INTERRUPT:
        heartbeat = max(1, int(expires_ts) - int(now))
        timeout = heartbeat + WAIT_GRACE_S
    return {"outcome": outcome, "heartbeat_s": heartbeat, "timeout_s": timeout}


def _staged_expiry(run: domain.Run) -> int:
    at = float(int(run.staged_pause.get("at") or 0))
    return run.next_pause_expiry(at, common.TTL_SECONDS)


def _quiet_failure(run: domain.Run, text: str) -> bool:
    try:
        service.record_failure(run, text)
    except domain.Conflict:
        return False
    return True


def _settled(
    approval_id: str, unknown: str = "TurnOutcomeUnknown", again: bool = True
) -> dict[str, Any]:
    """The outcome the row already holds, for a task that lost its claim
    or its race. Never invokes the agent."""
    now = time.time()
    run = service.unit_of_work().get(approval_id)
    if run is None:
        return _result(FAILED)
    if run.staged_pause:
        return _result(INTERRUPT, _staged_expiry(run), now)
    if run.status in _TERMINAL_OUTCOME:
        return _result(_TERMINAL_OUTCOME[run.status])
    if run.status == domain.PENDING:
        return _result(INTERRUPT, int(run.row.get("expires_ts") or 0), now)
    if run.status in {domain.STARTING, domain.RESUMING}:
        # Claimed by an earlier attempt that left no outcome: the agent
        # may have run, so nobody is asked to decide again.
        if _quiet_failure(run, f"{unknown}: the turn did not report back") or not again:
            return _result(FAILED)
        return _settled(approval_id, unknown, again=False)
    return _result(FAILED)


def _record(run: domain.Run, result: contract.AgentResult) -> dict[str, Any]:
    """Store one agent answer: ERROR, DONE or REFUSED_DONE, or a staged
    pause whose expiry gives the wait its bounds."""
    if result.is_error:
        service.record_failure(run, result.error)
        return _result(FAILED)
    if result.kind == contract.COMPLETED:
        status = service.record_completion(run, result.text)
        return _result(COMPLETED if status == domain.DONE else REFUSED)
    now = time.time()
    expires = run.next_pause_expiry(now, common.TTL_SECONDS)
    card = runner._pending_card(result.as_payload())
    card["expires_at"] = domain.iso(expires)
    service.stage_pause(run, result.interrupts, card, now)
    return _result(INTERRUPT, expires, now)


# --- AgentTurn --------------------------------------------------------


def _start_turn(run: domain.Run, context) -> dict[str, Any]:
    prompt = str(run.row.get("prompt", "") or "")
    if not prompt:
        service.record_failure(run, "MissingPrompt: the run row carries no prompt")
        return _result(FAILED)
    try:
        raw = runner._invoke_start(run.session_id, prompt, context)
    except Exception as exc:
        service.record_failure(run, common.error_text(exc))
        return _result(FAILED)
    return _record(run, contract.read_result(raw))


def _resume_turn(run: domain.Run, context) -> dict[str, Any]:
    decided = str(run.row.get("resumed_decision", ""))
    if decided not in {domain.APPROVED, domain.DECLINED}:
        service.record_failure(run, "ResumeWithoutDecision: nothing to answer the agent with")
        return _result(FAILED)
    answer = "y" if decided == domain.APPROVED else "no"
    try:
        raw = common.invoke_agent(
            run.session_id,
            {"session_id": run.session_id, "interrupt_responses": runner._answers(run, answer)},
            read_timeout=runner._read_timeout(context),
        )
    except Exception as exc:
        if common.failed_before_send(exc):
            # The agent was never invoked: the person may decide again, on
            # the same pause and its expiry, as on the runner path.
            if service.resume_not_sent(run) == domain.PENDING:
                return _result(INTERRUPT, int(run.row.get("expires_ts") or 0), time.time())
            return _result(FAILED)
        service.record_failure(run, common.error_text(exc))
        return _result(FAILED)
    return _record(run, contract.read_result(raw))


def turn(approval_id: str, context=None) -> dict[str, Any]:
    run = service.unit_of_work().get(approval_id)
    if run is None:
        return _result(FAILED)
    try:
        if run.status == domain.STARTING:
            claimed = service.claim_run(approval_id, runner._request_id(context))
            if claimed is None:
                return _settled(approval_id)
            return _start_turn(claimed, context)
        if run.status == domain.RESUMING:
            try:
                service.claim_resume(run)
            except domain.Conflict:
                return _settled(approval_id, "ResumeOutcomeUnknown")
            return _resume_turn(run, context)
    except domain.Conflict:
        return _settled(approval_id)
    return _settled(approval_id)


# --- WaitForDecision --------------------------------------------------


def _sfn() -> Any:
    """The Step Functions client (SendTaskFailure). A seam for the tests."""
    return boto3.client("stepfunctions", region_name=common.REGION)


def await_decision(approval_id: str, task_token: str) -> dict[str, Any]:
    try:
        run = service.await_decision(approval_id, task_token)
    except Exception as exc:
        # The token itself is never logged.
        _log(await_failed=type(exc).__name__)
        _sfn().send_task_failure(
            taskToken=task_token,
            error=type(exc).__name__[:256],
            cause="the pause could not be opened for a decision",
        )
        return {"status": "failed"}
    if run.lane == "approval":
        try:
            runner._send_card(run.row)
        except Exception as exc:  # the optional SES card never fails the wait
            _log(card_failed=type(exc).__name__)
    return {"status": "waiting"}


# --- ApplyDecision ----------------------------------------------------


def apply_decision(approval_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    work = service.unit_of_work()
    run = work.get(approval_id)
    if run is None:
        return _result(FAILED)
    if run.status == domain.RESUMING and run.row.get("resumed_decision") in {
        domain.APPROVED,
        domain.DECLINED,
    }:
        return _result(RESUME)  # a retried apply that already went through
    if run.status == domain.PENDING:
        try:
            service.record_decision(
                approval_id,
                run.lane,
                str(decision.get("decision", "")),
                str(decision.get("decided_by", "")),
                str(decision.get("decided_by_name", "")),
                now=float(decision.get("decided_ts") or time.time()),
                by_workflow=True,
            )
        except domain.Busy:
            raise  # transient contention: the state machine retries the task
        except domain.DomainError as exc:
            run = work.get(approval_id)
            moved = run is not None and run.status in {
                domain.APPROVED,
                domain.DECLINED,
                domain.RESUMING,
            }
            if not (isinstance(exc, domain.Conflict) and moved):
                # Past the deadline, or refused by the aggregate: the pause
                # ends the way an undecided one does, never left open.
                _log(decision_not_applied=type(exc).__name__)
                return _result(EXPIRED)
            if run.status == domain.RESUMING:
                return _result(RESUME)
        run = work.get(approval_id)
    if run is not None and run.status in {domain.APPROVED, domain.DECLINED}:
        try:
            service.resume(approval_id)
        except domain.Busy:
            raise  # transient contention: the state machine retries the task
        except domain.Conflict:
            run = work.get(approval_id)
            moved = run is not None and run.status == domain.RESUMING
            return _result(RESUME if moved else FAILED)
        return _result(RESUME)
    return _result(FAILED)


# --- Expire -----------------------------------------------------------


def _expiry_answer(run: domain.Run, answer: str, context) -> dict:
    payload = {"session_id": run.session_id, "interrupt_responses": runner._answers(run, answer)}
    for backoff in (*EXPIRY_BACKOFF_S, None):
        try:
            return common.invoke_agent(
                run.session_id, payload, read_timeout=runner._read_timeout(context)
            )
        except Exception as exc:
            if backoff is None or not common.failed_before_send(exc):
                raise
            _sleep(backoff)
    raise AssertionError("unreachable")  # pragma: no cover


def expire(approval_id: str, context=None) -> dict[str, Any]:
    work = service.unit_of_work()
    run = work.get(approval_id)
    if run is None:
        return _result(FAILED)
    try:
        if run.staged_pause and run.status in {domain.STARTING, domain.RESUMING}:
            # The wait timed out before its Lambda opened the pause.
            run.await_decision("", common.TTL_SECONDS)
            work.commit(run)
        if run.status != domain.PENDING:
            return _settled(approval_id, "ResumeOutcomeUnknown")
        run.clear_task_token()
        if run.lane == "try":
            service.expire(run)
            return _result(EXPIRED)
        answer = service.expiry_resume(run)
        try:
            raw = _expiry_answer(run, answer, context)
        except Exception as exc:
            service.record_failure(run, common.error_text(exc))
            return _result(FAILED)
        outcome = contract.read_result(raw)
        if outcome.is_error:
            service.record_failure(run, outcome.error)
            return _result(FAILED)
        service.expire(run)
        return _result(EXPIRED)
    except domain.Conflict:
        return _settled(approval_id, "ResumeOutcomeUnknown")


# --- RecordFailure ----------------------------------------------------


def record_failure(approval_id: str, error: str) -> dict[str, Any]:
    run = service.unit_of_work().get(approval_id)
    if run is None:
        return _result(FAILED)
    if run.status in _TERMINAL_OUTCOME:
        return _result(_TERMINAL_OUTCOME[run.status])
    label = domain.error_label(f"{error}:") if error else "WorkflowTaskFailed"
    if run.status in {domain.STARTING, domain.RESUMING}:
        with contextlib.suppress(domain.Conflict):
            service.record_failure(run, f"{label}: a workflow task failed")
        return _result(FAILED)
    _log(workflow_failure_unrecorded=run.status, error=label)
    return _result(FAILED)


def handler(event, context):
    action = event.get("action")
    approval_id = str(event.get("approval_id", ""))
    if action == "turn":
        return turn(approval_id, context)
    if action == "await":
        return await_decision(approval_id, str(event.get("task_token", "")))
    if action == "apply":
        decision = event.get("decision")
        return apply_decision(approval_id, decision if isinstance(decision, dict) else {})
    if action == "expire":
        return expire(approval_id, context)
    if action == "fail":
        return record_failure(approval_id, str(event.get("error", "")))
    return {"status": "error", "error": f"unknown action {action!r}"}

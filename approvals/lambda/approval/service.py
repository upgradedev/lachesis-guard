"""Application commands of the Approval bounded context.

Each command loads (or creates) one Run, asks the aggregate to change,
and commits through the Unit of Work: one command, one transaction. The
entrypoints (api.py, runner.py, sweeper.py) call these and do no state
writes of their own. Agent invocation stays in runner.py; this module
never calls the agent.

Commands that a caller may lose a race on raise domain.Conflict (or a
subclass); the caller decides whether that is a 409 or a no-op.
"""

from __future__ import annotations

import datetime
import time
from collections.abc import Iterable
from typing import Any

import common

from approval import domain, uow

COUNTER_TTL_SECONDS = 2 * 86400


def unit_of_work() -> uow.DynamoUnitOfWork:
    return uow.DynamoUnitOfWork()


def _now(now: float | None) -> float:
    return time.time() if now is None else now


# --- starting a run ---------------------------------------------------


def start_run(
    lane: str,
    *,
    counters: Iterable[uow.Counter] = (),
    now: float | None = None,
    orchestrator: str = "",
    prompt: str = "",
) -> domain.Run:
    """Write the STARTING row and RunRequested (and any counters) in one
    transaction, before anyone is told the token. The caller then hands
    the approval id to the runner, or, for a run with ``orchestrator``
    domain.WORKFLOW (B6), starts its lachesis-run execution."""
    run = domain.Run.request(
        common.new_approval_id(),
        common.new_session_id(),
        lane,
        _now(now),
        common.TTL_SECONDS,
        orchestrator=orchestrator,
        prompt=prompt,
    )
    return unit_of_work().commit(run, counters=counters)


def try_counters(source_ip: str, now: float | None = None) -> list[uow.Counter]:
    """The per-address and the global daily counter of the /try lane."""
    moment = _now(now)
    day = datetime.datetime.fromtimestamp(moment, datetime.timezone.utc).strftime("%Y-%m-%d")
    ttl = int(moment) + COUNTER_TTL_SECONDS
    return [
        uow.Counter(f"try-ip-{source_ip}-{day}", common.TRY_IP_DAILY_CAP, ttl),
        uow.Counter(f"try-counter-{day}", common.TRY_DAILY_CAP, ttl),
    ]


def start_try(
    source_ip: str, now: float | None = None, *, orchestrator: str = "", prompt: str = ""
) -> domain.Run:
    """POST /try: both daily counters, the STARTING row and RunRequested
    in one transaction. Raises domain.CapReached, consuming nothing, when
    either counter is at its cap."""
    return start_run(
        "try",
        counters=try_counters(source_ip, now),
        now=now,
        orchestrator=orchestrator,
        prompt=prompt,
    )


def record_gate(
    approval_id: str,
    project: str,
    gate: dict[str, Any],
    card: dict[str, Any] | None,
    now: float | None = None,
) -> domain.Run:
    """B7b: write a merge-gate run in one transaction, already decided by
    the gate: PENDING with ``card`` when a person must decide, otherwise
    DONE (the gate passed). The row, RunRequested and RunPaused or
    RunCompleted are one commit, so the run is never seen in STARTING.
    ``approval_id`` is the caller's (gate_api derives it from the pull
    request head); a run already at that id raises domain.Conflict and
    writes nothing."""
    moment = _now(now)
    run = domain.Run.request_gate(
        approval_id, common.new_session_id(), project, gate, moment, common.TTL_SECONDS
    )
    if card is None:
        run.pass_gate()
    else:
        run.hold_gate(card, moment, common.TTL_SECONDS)
    return unit_of_work().commit(run)


def claim_run(approval_id: str, request_id: str) -> domain.Run | None:
    """The runner takes a STARTING run before it invokes the agent.

    None means "do nothing": the row is missing, it is no longer
    STARTING, or another delivery of the same async event claimed it
    first (by an earlier commit, or by winning the transaction)."""
    work = unit_of_work()
    run = work.get(approval_id)
    if run is None:
        return None
    try:
        run.claim(request_id)
        return work.commit(run)
    except domain.Conflict:
        return None


# --- the run's outcomes -----------------------------------------------


def record_pause(
    run: domain.Run, interrupts: list[dict], card: dict, now: float | None = None
) -> domain.Run:
    run.pause(interrupts, card, _now(now), common.TTL_SECONDS)
    return unit_of_work().commit(run)


def record_completion(run: domain.Run, text: str) -> str:
    status = run.finish(text)
    unit_of_work().commit(run)
    return status


def record_failure(run: domain.Run, text: str) -> domain.Run:
    run.fail(text)
    return unit_of_work().commit(run)


# --- a person's decision ----------------------------------------------


def record_decision(
    approval_id: str,
    lane: str | Iterable[str],
    decision: str,
    decided_by: str,
    decided_by_name: str,
    now: float | None = None,
    *,
    by_workflow: bool = False,
) -> domain.Run:
    """PENDING -> APPROVED or DECLINED, with DecisionRecorded.

    ``lane`` is one lane or several (B7b: the approver route decides the
    approval and the gate lane alike). Raises NotFound when there is no
    such run on ``lane``, Expired,
    NotResumable (a fail-closed pause on the public lane), or Conflict
    when the run is not PENDING or moved before the commit.

    A run the lachesis-run workflow owns (B6) is decided only by the
    workflow (``by_workflow``, its ApplyDecision task, which also drops
    the used task token). Anyone else gets domain.WorkflowOwned, after
    every refusal above was checked on a copy and nothing was written, so
    the API can hand the decision to the workflow instead."""
    lanes = {lane} if isinstance(lane, str) else set(lane)
    work = unit_of_work()
    run = work.get(approval_id)
    if run is None or run.lane not in lanes:
        raise domain.NotFound()
    if run.workflow_owned and not by_workflow:
        moment = _now(now)
        domain.Run.from_item(run.row).decide(decision, decided_by, decided_by_name, moment)
        raise domain.WorkflowOwned(run, moment)
    run.decide(decision, decided_by, decided_by_name, _now(now))
    if by_workflow:
        run.clear_task_token()
    return work.commit(run)


# --- resuming ---------------------------------------------------------


def resume(approval_id: str, now: float | None = None) -> tuple[domain.Run, str]:
    """APPROVED or DECLINED -> RESUMING. Returns the run and the answer
    for the agent ("y" or "no"). Raises NotFound, InvalidTransition (not
    decided) or Conflict (another resume moved it first)."""
    work = unit_of_work()
    run = work.get(approval_id)
    if run is None:
        raise domain.NotFound()
    answer = run.begin_resume(_now(now))
    work.commit(run)
    return run, answer


def resume_not_sent(run: domain.Run, now: float | None = None) -> str:
    """Record a resume that failed before the agent was invoked."""
    status = run.resume_not_sent(_now(now))
    unit_of_work().commit(run)
    return status


def expiry_resume(run: domain.Run, now: float | None = None) -> str:
    """An expired PENDING run moves to RESUMING to be resumed with "no"."""
    answer = run.begin_expiry_resume(_now(now))
    unit_of_work().commit(run)
    return answer


def expire(run: domain.Run) -> domain.Run:
    """PENDING (public lane) or RESUMING (after the expiry resume) -> EXPIRED."""
    run.expire()
    return unit_of_work().commit(run)


# --- B6: the lachesis-run workflow's own commands ---------------------


def claim_resume(run: domain.Run) -> domain.Run:
    """AgentTurn takes a RESUMING run once (domain.Run.claim_resume)."""
    run.claim_resume()
    return unit_of_work().commit(run)


def stage_pause(run: domain.Run, interrupts: list[dict], card: dict, now: float) -> domain.Run:
    run.stage_pause(interrupts, card, now)
    return unit_of_work().commit(run)


def await_decision(approval_id: str, task_token: str) -> domain.Run:
    """WaitForDecision: the pause and its task token in one write.
    Raises NotFound, InvalidTransition or Conflict."""
    work = unit_of_work()
    run = work.get(approval_id)
    if run is None:
        raise domain.NotFound()
    run.await_decision(task_token, common.TTL_SECONDS)
    return work.commit(run)

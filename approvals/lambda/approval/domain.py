"""The Run aggregate of the Approval bounded context.

A Run is one agent run that Lachesis may pause for a person. It is one
row of the lachesis-approvals table; this module owns every rule about
how that row may change, and nothing else. It is pure Python: no boto3,
no clock (callers pass ``now`` as epoch seconds), no I/O, so the rules
can be tested on their own.

Every command method checks the move against TRANSITIONS, changes the
in-memory row, and records the domain events the move produced. The Unit
of Work (approval/uow.py) then writes the changed attributes and the new
events in one transaction, conditioned on the status and the version the
Run was loaded at, and calls ``committed``. The version goes up by one
per committed command; each event takes the next per-run ``seq``, so the
event log of a run is contiguous and totally ordered.

Events carry only the whitelisted fields their dataclass declares: never
tool arguments, the Confirm prompt text, the agent prompt, interrupt
ids, task tokens, IP addresses, the approver's Cognito sub, or raw
exception text (a failure carries only the exception type name).
"""

from __future__ import annotations

import datetime
import json
import math
import re
from dataclasses import dataclass, field, fields
from typing import Any

STARTING = "STARTING"
PENDING = "PENDING"
APPROVED = "APPROVED"
DECLINED = "DECLINED"
RESUMING = "RESUMING"
DONE = "DONE"
REFUSED_DONE = "REFUSED_DONE"
EXPIRED = "EXPIRED"
ERROR = "ERROR"

STATUSES = (
    STARTING,
    PENDING,
    APPROVED,
    DECLINED,
    RESUMING,
    DONE,
    REFUSED_DONE,
    EXPIRED,
    ERROR,
)
TERMINAL = frozenset({DONE, REFUSED_DONE, EXPIRED, ERROR})

# The allowed moves, and the only ones. STARTING -> STARTING is the
# claim: the runner takes the run before it invokes the agent, without a
# new status (the console knows these nine and no others).
TRANSITIONS: dict[str, frozenset[str]] = {
    STARTING: frozenset({STARTING, PENDING, DONE, ERROR}),
    PENDING: frozenset({APPROVED, DECLINED, RESUMING, EXPIRED}),
    APPROVED: frozenset({RESUMING}),
    DECLINED: frozenset({RESUMING}),
    RESUMING: frozenset({PENDING, DONE, REFUSED_DONE, EXPIRED, ERROR}),
    DONE: frozenset(),
    REFUSED_DONE: frozenset(),
    EXPIRED: frozenset(),
    ERROR: frozenset(),
}

# A resume that failed before the agent was invoked may be tried again;
# the third such failure is terminal.
MAX_RESUME_ATTEMPTS = 3

# Clears the current-decision fields when a pause ends with nobody
# deciding it. A re-pause keeps the previous pause's decider in the row;
# without this, an expiry would be credited to that person. The earlier
# decisions stay in ``decisions``.
NOBODY_DECIDED = {"decided_by": "", "decided_by_name": "", "decided_at": ""}

# The orchestrator marker of a run the lachesis-run state machine owns
# (B6). Written only when USE_STEP_FUNCTIONS is on; a row without it is a
# runner-owned run, exactly as before B6.
WORKFLOW = "sfn"

# The merge-gate lane (B7b): one run per pull-request head, recorded by
# gate_api.py. A gate run never has an agent behind it: it is written
# already PENDING (a person decides) or DONE (the gate passed), a decision
# ends it APPROVED or DECLINED, and nothing ever resumes it.
GATE = "gate"
# A gate run's row lives this long (DynamoDB TTL), well past its pause:
# a re-run of the job or a reopened pull request days later must still
# find the run of that head and its decision, never a free id. The rows
# are small, and the event log keeps 30 days too.
GATE_ROW_TTL_SECONDS = 30 * 86400

_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")
_TEXT_CAP = 64


def error_label(text: str) -> str:
    """Only the exception type name of a stored error: the text before the
    first colon, and only when it looks like an identifier."""
    if not text:
        return ""
    prefix = str(text).split(":", 1)[0].strip()
    return prefix if _TYPE_NAME.fullmatch(prefix) else "Error"


def iso(now: float) -> str:
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(
        timespec="seconds"
    )


# --- errors -----------------------------------------------------------


class DomainError(Exception):
    """A command the aggregate refuses. ``http_status`` is what the API
    answers; the message is the API's public error text."""

    http_status = 409
    default_message = "the run is not in a state that allows this"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.default_message)


class Conflict(DomainError):
    """The run moved under the command: its status or version is not the
    one the command was decided on."""

    default_message = "approval is no longer pending"


class InvalidTransition(Conflict):
    """The move is not in TRANSITIONS from the run's current status,
    which ``current`` names."""

    def __init__(self, message: str = "", current: str = "") -> None:
        super().__init__(message)
        self.current = current


class Busy(Conflict):
    """Concurrent transactions kept cancelling the command (contention
    on a shared item, such as the /try daily counter), and the retries
    ran out. Nothing was written. A Conflict to every caller that treats
    a lost race as a no-op; the API answers it as a retryable 503."""

    http_status = 503
    default_message = "busy, try again in a moment"


class AlreadyClaimed(Conflict):
    """A runner already claimed this run (a duplicate async delivery)."""

    default_message = "run already claimed"


class NotDecidable(Busy):
    """A workflow-owned run is PENDING, but its task token is not on the
    row yet. Cannot happen when the token and the pause are one write
    (Run.await_decision); kept as a retryable 503 for a pause that a
    pre-send resume failure returned to PENDING (B6)."""

    default_message = "the approval is not open for a decision yet, try again in a moment"


class WorkflowOwned(Conflict):
    """The run is owned by the lachesis-run state machine (B6): only the
    workflow may record its decision. Carries the loaded run so the API
    can hand the decision to the workflow instead, and ``now``, the
    moment the refusals were checked at, which the workflow records as
    the decision time."""

    def __init__(self, run: Run, now: float) -> None:
        super().__init__("the run is owned by the workflow")
        self.run = run
        self.now = now


class Expired(DomainError):
    default_message = "approval has expired"


class NotResumable(DomainError):
    default_message = (
        "Decider GPU is cold (scale-to-zero); "
        "a fail-closed pause on the public lane cannot be resumed"
    )


class NotFound(DomainError):
    http_status = 404
    default_message = "unknown approval"


class CapReached(DomainError):
    http_status = 429
    default_message = (
        "daily demo cap reached for this address or overall; "
        "the counters reset at 00:00 UTC"
    )


# --- events -----------------------------------------------------------


def _text(value: Any) -> str:
    return str(value if value is not None else "")[:_TEXT_CAP]


def _number(value: Any) -> float | None:
    """A finite number or None: DynamoDB refuses NaN and Infinity, and an
    event field must never fail the transaction that carries it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class DomainEvent:
    """Base of every event. The dataclass fields are the whitelist."""

    @property
    def name(self) -> str:
        return type(self).__name__

    def payload(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(frozen=True)
class RunRequested(DomainEvent):
    lane: str


@dataclass(frozen=True)
class RunClaimed(DomainEvent):
    request_id: str


@dataclass(frozen=True)
class RunPaused(DomainEvent):
    pause_seq: int
    tool: str
    risk: str
    confidence: float | None
    layer: str
    latency_ms: float | None
    expires_ts: int
    # True when the pausing row is the handler's cached Confirm,
    # re-issued on resume with no new model call (B3).
    cached: bool = False


@dataclass(frozen=True)
class DecisionRecorded(DomainEvent):
    pause_seq: int
    decision: str
    decided_by_name: str


@dataclass(frozen=True)
class RunResuming(DomainEvent):
    pause_seq: int
    answer: str
    cause: str


@dataclass(frozen=True)
class ResumeNotSent(DomainEvent):
    pause_seq: int
    attempts: int


@dataclass(frozen=True)
class RunCompleted(DomainEvent):
    pause_seq: int


@dataclass(frozen=True)
class RunRefused(DomainEvent):
    pause_seq: int


@dataclass(frozen=True)
class RunExpired(DomainEvent):
    pause_seq: int
    resumed: bool


@dataclass(frozen=True)
class RunFailed(DomainEvent):
    pause_seq: int
    from_status: str
    error_type: str


EVENT_TYPES = (
    RunRequested,
    RunClaimed,
    RunPaused,
    DecisionRecorded,
    RunResuming,
    ResumeNotSent,
    RunCompleted,
    RunRefused,
    RunExpired,
    RunFailed,
)


# --- the aggregate ----------------------------------------------------


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class Run:
    """The aggregate root. ``row`` is the whole stored row as last
    loaded, with this command's changes applied in memory."""

    row: dict[str, Any]
    loaded_status: str
    loaded_version: int
    is_new: bool = False
    changes: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[int, DomainEvent]] = field(default_factory=list)
    must_be_absent: list[str] = field(default_factory=list)

    # construction

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Run:
        row = dict(item)
        # Rows written before the Unit of Work have no version or seq
        # counters; DynamoDB returns numbers as Decimal.
        for key in ("version", "pause_seq", "event_seq"):
            row[key] = _int(row.get(key))
        return cls(row=row, loaded_status=str(row["status"]), loaded_version=row["version"])

    @classmethod
    def request(
        cls,
        approval_id: str,
        session_id: str,
        lane: str,
        now: float,
        ttl_seconds: int,
        *,
        orchestrator: str = "",
        prompt: str = "",
        project: str = "",
    ) -> Run:
        """A new run in STARTING, written before the agent is invoked.

        ``orchestrator`` and ``prompt`` are given only for a run the
        lachesis-run workflow owns (B6): the workflow's first AgentTurn
        reads the prompt from the row, so the execution state carries the
        approval id only. Without them the row is exactly the pre-B6 row.
        ``project`` (B7b) is the registry name the console groups the run
        under; without it the row has no project attribute."""
        created = int(now)
        row = {
            "approval_id": approval_id,
            "session_id": session_id,
            "status": STARTING,
            "lane": lane,
            "card": "{}",
            "interrupts": "[]",
            "created_ts": created,
            "expires_ts": created + ttl_seconds,
            "ttl": created + ttl_seconds + 86400,
            "version": 0,
            "pause_seq": 0,
            "event_seq": 0,
        }
        if orchestrator:
            row["orchestrator"] = str(orchestrator)
            row["prompt"] = str(prompt)
        if project:
            row["project"] = str(project)
        run = cls(row=row, loaded_status="", loaded_version=0, is_new=True)
        run._record(RunRequested(lane=_text(lane)))
        return run

    @classmethod
    def request_gate(
        cls,
        approval_id: str,
        session_id: str,
        project: str,
        gate: dict[str, Any],
        now: float,
        ttl_seconds: int,
    ) -> Run:
        """A new merge-gate run (B7b), still in memory and STARTING. The
        caller moves it on with pass_gate() or hold_gate() before the one
        commit that writes it, so the row is never seen in STARTING.
        ``gate`` holds the repository, the pull request, its head sha, its
        base branch (base_ref, already escaped for display: the run id
        holds the branch itself) and the verdict summary GET /gate/{id}
        answers with."""
        run = cls.request(approval_id, session_id, GATE, now, ttl_seconds, project=project)
        run.row.update(
            repository=str(gate.get("repository", "")),
            pr_number=_int(gate.get("pr_number")),
            head_sha=str(gate.get("head_sha", "")),
            base_ref=str(gate.get("base_ref", "")),
            gate=json.dumps(gate.get("summary") or {}, sort_keys=True),
            ttl=int(now) + GATE_ROW_TTL_SECONDS,
        )
        return run

    # read side

    @property
    def approval_id(self) -> str:
        return str(self.row["approval_id"])

    @property
    def status(self) -> str:
        return str(self.row["status"])

    @property
    def version(self) -> int:
        return int(self.row["version"])

    @property
    def lane(self) -> str:
        return str(self.row.get("lane", "approval") or "approval")

    @property
    def is_gate(self) -> bool:
        return self.lane == GATE

    @property
    def session_id(self) -> str:
        return str(self.row.get("session_id", ""))

    @property
    def pause_seq(self) -> int:
        return int(self.row["pause_seq"])

    @property
    def event_seq(self) -> int:
        return int(self.row["event_seq"])

    @property
    def card(self) -> dict[str, Any]:
        try:
            card = json.loads(self.row.get("card") or "{}")
        except (TypeError, ValueError):
            return {}
        return card if isinstance(card, dict) else {}

    @property
    def interrupts(self) -> list[dict[str, Any]]:
        try:
            items = json.loads(self.row.get("interrupts") or "[]")
        except (TypeError, ValueError):
            return []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def is_expired(self, now: float) -> bool:
        expires = _int(self.row.get("expires_ts"))
        return bool(expires) and expires < int(now)

    # B6: the workflow-owned run

    @property
    def orchestrator(self) -> str:
        return str(self.row.get("orchestrator", "") or "")

    @property
    def workflow_owned(self) -> bool:
        return self.orchestrator == WORKFLOW

    @property
    def task_token(self) -> str:
        """The WaitForDecision task token. Never in public_view, the
        ledger or an event: only the API reads it, to answer a decision
        with SendTaskSuccess."""
        return str(self.row.get("task_token", "") or "")

    @property
    def staged_pause(self) -> dict[str, Any]:
        """The pause an AgentTurn staged for WaitForDecision, or {}."""
        try:
            stage = json.loads(self.row.get("staged_pause") or "{}")
        except (TypeError, ValueError):
            return {}
        return stage if isinstance(stage, dict) else {}

    def next_pause_expiry(self, now: float, ttl_seconds: int) -> int:
        """The expires_ts the next pause will carry, by the rule pause()
        applies: a first pause keeps the expiry the run was created with,
        a re-pause from RESUMING renews it from ``now``."""
        if self.status == RESUMING:
            return int(now) + ttl_seconds
        return _int(self.row.get("expires_ts")) or int(now) + ttl_seconds

    # write side helpers

    def _set(self, **attrs: Any) -> None:
        self.row.update(attrs)
        self.changes.update(attrs)

    def _move(self, target: str, **attrs: Any) -> None:
        if target not in TRANSITIONS.get(self.status, frozenset()):
            raise InvalidTransition(f"{self.status} -> {target} is not allowed", self.status)
        self._set(status=target, **attrs)

    def _record(self, event: DomainEvent) -> None:
        seq = self.event_seq + 1
        self.row["event_seq"] = seq
        self.changes["event_seq"] = seq
        self.events.append((seq, event))

    def committed(self) -> None:
        """Called by the Unit of Work once the transaction succeeded."""
        self.row["version"] = self.loaded_version + 1
        self.loaded_version = self.row["version"]
        self.loaded_status = self.status
        self.is_new = False
        self.changes = {}
        self.events = []
        self.must_be_absent = []

    def _history(self) -> list[dict[str, Any]]:
        history = self.row.get("decisions")
        return list(history) if isinstance(history, list) else []

    def _decision_entry(self, decided: str, now: float, suffix: str = "") -> dict[str, str]:
        """One history entry for the decision a resume consumed. The
        Cognito sub is not copied: the history is public, the sub is not."""
        return {
            "decision": ("approve" if decided == APPROVED else "decline") + suffix,
            "decided_by_name": str(self.row.get("decided_by_name", "")),
            "at": str(self.row.get("decided_at") or iso(now)),
        }

    # commands

    def claim(self, request_id: str) -> None:
        """The runner takes a STARTING run before it invokes the agent. A
        second claim (a duplicate async delivery) is refused."""
        if self.status != STARTING:
            raise InvalidTransition(f"cannot claim a run in {self.status}", self.status)
        if self.row.get("claimed_request_id"):
            raise AlreadyClaimed()
        self._move(STARTING, claimed_request_id=str(request_id))
        self.must_be_absent.append("claimed_request_id")
        self._record(RunClaimed(request_id=_text(request_id)))

    def pause(
        self, interrupts: list[dict], card: dict[str, Any], now: float, ttl_seconds: int
    ) -> None:
        """Lachesis paused a tool call. From RESUMING this is a re-pause:
        the decision just consumed joins ``decisions``, decided_by and
        decided_by_name are kept (public_view hides them while PENDING),
        and the expiry window is renewed."""
        attrs: dict[str, Any] = {
            "interrupts": json.dumps(interrupts),
            "card": json.dumps(card),
            "pause_seq": self.pause_seq + 1,
        }
        if self.status == RESUMING:
            decided = str(self.row.get("resumed_decision", ""))
            history = self._history()
            if decided in {APPROVED, DECLINED}:
                history.append(self._decision_entry(decided, now))
            attrs.update(
                expires_ts=int(now) + ttl_seconds,
                ttl=int(now) + ttl_seconds + 86400,
                decisions=history,
                resumed_decision="",
            )
        self._move(PENDING, **attrs)
        self._record(
            RunPaused(
                pause_seq=self.pause_seq,
                tool=_text(card.get("tool", "")),
                risk=_text(card.get("risk", "")),
                confidence=_number(card.get("confidence")),
                layer=_text(card.get("layer", "")),
                latency_ms=_number(card.get("latency_ms")),
                expires_ts=_int(self.row.get("expires_ts")),
                cached=card.get("cached") is True,
            )
        )

    def decide(self, decision: str, decided_by: str, decided_by_name: str, now: float) -> None:
        """A person answers the current pause: approve or decline."""
        if decision not in {"approve", "decline"}:
            raise DomainError("decision must be approve or decline")
        if self.is_expired(now):
            raise Expired()
        if self.lane == "try" and self.card.get("layer") == "unavailable":
            raise NotResumable()
        if self.status != PENDING:
            raise Conflict()
        self._move(
            APPROVED if decision == "approve" else DECLINED,
            decided_by=str(decided_by),
            decided_by_name=str(decided_by_name),
            decided_at=iso(now),
        )
        self._record(
            DecisionRecorded(
                pause_seq=self.pause_seq,
                decision=decision,
                decided_by_name=_text(decided_by_name),
            )
        )

    def _no_agent(self, what: str) -> None:
        """A gate run has no agent session: nothing may resume it."""
        if self.is_gate:
            raise InvalidTransition(f"a gate run is never {what}", self.status)

    def pass_gate(self) -> None:
        """B7b: the gate found nothing a person must see. STARTING -> DONE
        in the same commit that writes the new row."""
        if not self.is_gate:
            raise InvalidTransition("only a gate run can pass the gate", self.status)
        self.finish("")

    def hold_gate(self, card: dict[str, Any], now: float, ttl_seconds: int) -> None:
        """B7b: the gate flagged a change, so a person decides. STARTING ->
        PENDING with the card, in the same commit that writes the row."""
        if not self.is_gate:
            raise InvalidTransition("only a gate run can hold the gate", self.status)
        self.pause([], card, now, ttl_seconds)

    def begin_resume(self, now: float) -> str:
        """APPROVED or DECLINED -> RESUMING. Returns the agent answer."""
        self._no_agent("resumed")
        decided = self.status
        if decided not in {APPROVED, DECLINED}:
            raise InvalidTransition(f"cannot resume a run in {decided}", decided)
        self._move(RESUMING, resuming_ts=int(now), resumed_decision=decided)
        self._record(
            RunResuming(
                pause_seq=self.pause_seq,
                answer="approve" if decided == APPROVED else "decline",
                cause="decision",
            )
        )
        return "y" if decided == APPROVED else "no"

    def begin_expiry_resume(self, now: float) -> str:
        """An expired PENDING pause is resumed with "no"; nobody decided it."""
        self._no_agent("resumed")
        self._move(RESUMING, resuming_ts=int(now), resumed_decision="", **NOBODY_DECIDED)
        self._record(RunResuming(pause_seq=self.pause_seq, answer="decline", cause="expiry"))
        return "no"

    def resume_not_sent(self, now: float) -> str:
        """A resume failed before the agent was invoked. Below the attempt
        cap the run returns to PENDING and a person's decision, made but
        never sent, joins ``decisions`` as "<choice>_not_sent" with the
        current-decision fields cleared. Returns the new status."""
        attempts = _int(self.row.get("sweep_attempts")) + 1
        if attempts >= MAX_RESUME_ATTEMPTS:
            from_status = self.status
            self._move(ERROR, sweep_attempts=attempts)
            self._record(
                RunFailed(
                    pause_seq=self.pause_seq, from_status=from_status, error_type="ResumeNotSent"
                )
            )
            return ERROR
        attrs: dict[str, Any] = {"sweep_attempts": attempts, "resumed_decision": ""}
        decided = str(self.row.get("resumed_decision", ""))
        if decided in {APPROVED, DECLINED}:
            attrs.update(
                decisions=[*self._history(), self._decision_entry(decided, now, "_not_sent")],
                **NOBODY_DECIDED,
            )
        self._move(PENDING, **attrs)
        self._record(ResumeNotSent(pause_seq=self.pause_seq, attempts=attempts))
        return PENDING

    def finish(self, text: str) -> str:
        """The agent completed. From STARTING the run is DONE; after a
        resume, DONE follows an approval and REFUSED_DONE a decline.
        Returns the new status."""
        if self.status == STARTING:
            target = DONE
        elif self.status == RESUMING:
            decided = str(self.row.get("resumed_decision", ""))
            if decided not in {APPROVED, DECLINED}:
                raise InvalidTransition(
                    "a completed resume needs the decision it consumed", self.status
                )
            target = DONE if decided == APPROVED else REFUSED_DONE
        else:
            raise InvalidTransition(f"cannot finish a run in {self.status}", self.status)
        self._move(target, result_text=str(text))
        event = RunCompleted if target == DONE else RunRefused
        self._record(event(pause_seq=self.pause_seq))
        return target

    def expire(self) -> None:
        """An undecided pause ends. From PENDING (the public lane, which
        never re-invokes the agent) nobody is credited; from RESUMING the
        expiry resume with "no" has returned."""
        resumed = self.status == RESUMING
        attrs = {"resumed_decision": ""} if resumed else dict(NOBODY_DECIDED)
        self._move(EXPIRED, **attrs)
        self._record(RunExpired(pause_seq=self.pause_seq, resumed=resumed))

    def fail(self, text: str) -> None:
        """The run ends in ERROR. ``text`` is stored for the owner; the
        event and public_view carry only its exception type name."""
        from_status = self.status
        self._move(ERROR, result_text=str(text))
        self._record(
            RunFailed(
                pause_seq=self.pause_seq, from_status=from_status, error_type=error_label(text)
            )
        )

    # B6: commands of the lachesis-run workflow. Each is one Unit of Work
    # commit, guarded like every other command by the status and version
    # the run was loaded at. None of them moves the status outside
    # TRANSITIONS: claim_resume, stage_pause and clear_task_token change
    # attributes only, await_decision moves through pause().

    def claim_resume(self) -> None:
        """AgentTurn takes a RESUMING run before it sends the answer to
        the agent, so a retried turn never resumes twice. The claim is
        the run's event_seq: the RunResuming of the next decision moves
        it, so each resume can be claimed exactly once."""
        if self.status != RESUMING:
            raise InvalidTransition(f"cannot claim a resume in {self.status}", self.status)
        mark = str(self.event_seq)
        if str(self.row.get("resume_claim", "")) == mark:
            raise AlreadyClaimed()
        self._set(resume_claim=mark)

    def stage_pause(self, interrupts: list[dict], card: dict[str, Any], now: float) -> None:
        """AgentTurn got an interrupt. The pause is kept on the row, not
        yet decidable: the status stays STARTING or RESUMING until
        await_decision writes the pause and the task token together."""
        if PENDING not in TRANSITIONS.get(self.status, frozenset()):
            raise InvalidTransition(f"cannot stage a pause in {self.status}", self.status)
        if self.staged_pause:
            raise InvalidTransition("a pause is already staged", self.status)
        self._set(
            staged_pause=json.dumps(
                {"interrupts": list(interrupts), "card": dict(card), "at": int(now)}
            )
        )

    def await_decision(self, task_token: str, ttl_seconds: int) -> None:
        """WaitForDecision: the staged pause becomes PENDING in the same
        write that stores the task token, so a person can never decide a
        pause the workflow cannot hear about. The pause is written by
        pause() at the moment it was staged, so its expires_ts is the one
        AgentTurn gave the wait's timeout. A PENDING run without a stage
        (a resume that failed before it was sent, or a retried wait) only
        takes the new token."""
        stage = self.staged_pause
        if stage:
            interrupts = stage.get("interrupts")
            card = stage.get("card")
            self.pause(
                interrupts if isinstance(interrupts, list) else [],
                card if isinstance(card, dict) else {},
                float(_int(stage.get("at"))),
                ttl_seconds,
            )
            self._set(staged_pause="", task_token=str(task_token))
            return
        if self.status != PENDING:
            raise InvalidTransition(f"nothing to wait for in {self.status}", self.status)
        self._set(task_token=str(task_token))

    def clear_task_token(self) -> None:
        """Drop a used or dead task token, in the command that leaves
        PENDING (an applied decision, an expiry)."""
        if self.task_token:
            self._set(task_token="")

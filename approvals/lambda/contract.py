"""Tolerant reader of the AgentCore result payload (anti-corruption layer).

The agent (agent/entrypoint.py) answers a handled invocation with HTTP
200 and one JSON object whose ``status`` is one of the three below. An
exception that escapes the agent is answered with HTTP 500 and a
``status: error`` body; depending on how InvokeAgentRuntime surfaces
it, that reaches the runner as an exception (handled by the runner as
a failure after the send) or as an error body (handled here). Both end
in ERROR. The statuses:

- ``"interrupt"``: Lachesis paused a tool call; ``interrupts`` holds
  ``[{interruptId, name, reason}]`` plus ``triage`` and ``changed_files``.
- ``"completed"``: the agent finished; ``text`` holds its final answer.
- ``"error"``: the agent refused the request (for example a short
  session id, or no prompt); ``error`` holds a plain message. These come
  back with HTTP 200, so a reader that only checks for ``interrupt``
  would record them as a finished run.

This module turns that payload into one of three kinds the runner can
trust, and nothing else:

- ``INTERRUPT`` only when ``status`` is ``interrupt`` and at least one
  interrupt carries an ``interruptId`` (a pause nobody can resume is an
  error, not a pause);
- ``COMPLETED`` only when ``status`` is ``completed``;
- ``ERROR`` for everything else: an ``error`` body, a missing or unknown
  status, a payload that is not a JSON object.

It is pure: no boto3, no table access, so it can be tested on its own
and reused by the sweeper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

INTERRUPT = "interrupt"
COMPLETED = "completed"
ERROR = "error"


@dataclass(frozen=True)
class AgentResult:
    kind: str
    text: str = ""
    error: str = ""
    interrupts: list[dict] = field(default_factory=list)
    triage: list[dict] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    @property
    def is_error(self) -> bool:
        return self.kind == ERROR

    def as_payload(self) -> dict[str, Any]:
        """The normalised payload the runner's card builder reads."""
        return {
            "status": self.kind,
            "text": self.text,
            "interrupts": list(self.interrupts),
            "triage": list(self.triage),
            "changed_files": list(self.changed_files),
        }


def _dicts(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def read_result(payload: Any) -> AgentResult:
    """Classify one agent payload. Never raises."""
    if not isinstance(payload, dict):
        return AgentResult(ERROR, error=f"MalformedResult: {type(payload).__name__} payload")
    status = payload.get("status")
    triage = _dicts(payload.get("triage"))
    changed = _strings(payload.get("changed_files"))
    if status == INTERRUPT:
        interrupts = [
            item for item in _dicts(payload.get("interrupts")) if item.get("interruptId")
        ]
        if not interrupts:
            return AgentResult(
                ERROR,
                error="MalformedResult: interrupt without an interruptId",
                triage=triage,
                changed_files=changed,
            )
        return AgentResult(
            INTERRUPT, interrupts=interrupts, triage=triage, changed_files=changed
        )
    if status == COMPLETED:
        text = payload.get("text")
        return AgentResult(
            COMPLETED,
            text=text if isinstance(text, str) else "",
            triage=triage,
            changed_files=changed,
        )
    if status == ERROR:
        message = payload.get("error")
        detail = message if isinstance(message, str) and message else "no message"
        return AgentResult(ERROR, error=f"AgentError: {detail}", triage=triage)
    return AgentResult(ERROR, error=f"UnknownStatus: {status!r}", triage=triage)


def final_status(result: AgentResult, decision_status: str) -> str:
    """Terminal row status after a resume.

    ``decision_status`` is the row status the resume consumed (APPROVED
    or DECLINED). An error result is ERROR whatever the decision was; a
    completed result is DONE after an approval and REFUSED_DONE after a
    decline. Any other decision value is ERROR: the runner cannot say
    what ran.
    """
    if result.kind != COMPLETED:
        return "ERROR"
    if decision_status == "APPROVED":
        return "DONE"
    if decision_status == "DECLINED":
        return "REFUSED_DONE"
    return "ERROR"

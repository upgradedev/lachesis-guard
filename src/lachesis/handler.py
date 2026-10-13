"""The handler: a Strands InterventionHandler that scores every tool call.

``before_tool_call`` runs before Strands executes the tool. It renders the
conversation plus the proposed call into a state, asks Decider both questions
in one round trip, and returns the SDK action the policy verdict maps to:

- proceed -> Proceed: the call runs.
- guide -> Guide: the model sees the feedback and typically asks the user.
- confirm -> Confirm: breaks out of the agent loop to pause for external
  resume. Only supported on beforeToolCall, which is the only seam this
  handler touches.
- deny -> Deny: the call is refused with the reason shown to the model.

If the Decider server is down, the error propagates and the run stops: an
agent that cannot be triaged does not run unguarded. ``on_error`` keeps the
SDK default (throw), which is the fail-closed direction.

Pause binding: a Confirm verdict is cached under the call's toolUseId, in
``agent.state`` when the agent has one (it survives process death through the
session manager) else in the handler. On re-entry after the pause the SAME
Confirm goes back without calling Decider, so the stored human answer is the
one consulted: a re-score that flips to Proceed would run the call the person
refused. Refused calls are remembered by hash, and an exact repeat is denied
outright. What the model sees stays neutral: Deny reasons and Guide feedback
carry no score, and the SDK's denied-Confirm echo (which quotes the whole
human prompt) is rewritten after the fact to "a person declined this call".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from strands.interventions import (
    Confirm,
    Deny,
    Guide,
    InterventionHandler,
    Proceed,
    Transform,
)

from lachesis.client import DeciderClient
from lachesis.policy import (
    DEFAULT_THRESHOLD,
    DENY,
    GUIDE,
    PROCEED,
    TIER0_READ_TOOLS,
    Verdict,
    decide,
    prescan,
)
from lachesis.questions import lachesis_questions
from lachesis.render import DEFAULT_MAX_TOKENS, render_state

VerdictCallback = Callable[[Verdict, dict[str, Any]], None]
"""Called after every triage with the verdict and the logged row."""


PROMPT_ARGS_CHARS = 2000
"""The cap reads keep in a Confirm prompt. Writes show the whole payload."""


STATE_VERDICTS = "lachesis_verdicts"
"""agent.state key: toolUseId to the Confirm record the pause was bound to."""

STATE_REFUSALS = "lachesis_refusals"
"""agent.state key: sha256(tool, args) to True for every refused call."""

STATE_PAUSED = "lachesis_paused"
"""agent.state key: toolUseId to True while a pause for it is outstanding."""

STATE_CAP = 512
"""Oldest entries drop past this many; a session never holds more pauses."""

CONFIRM_DENIED_PREFIX = "CONFIRMATION_FAILED:"
"""How the SDK marks a human-denied Confirm in the tool result: the cancel
message is f"CONFIRMATION_FAILED: {prompt}" (strands
``interventions/registry.py``). The rewrite below keys on it; if a future SDK
renames it, the rewrite quietly stops and the model sees the prompt again."""

REFUSAL_MESSAGE = "Lachesis refuses: this exact call was already refused."
"""Deny text for an exact repeat of a refused call. No numbers: Deny reasons
reach the model verbatim (the SDK prefixes "DENIED: ")."""


def format_arguments(arguments: Any, limit: int | None = None) -> str:
    """The proposed arguments as pretty JSON with real newlines.

    Full by default: a person approving a write sees the whole payload, tail
    included. Reads pass ``limit=PROMPT_ARGS_CHARS`` to keep the cap.
    """
    try:
        text = json.dumps(arguments, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(arguments)
    if limit is not None and len(text) > limit:
        return text[:limit] + f"... [{len(text) - limit} more characters]"
    return text


def _confirm_prompt(tool_name: str, arguments: Any, verdict: Verdict, threshold: float) -> str:
    """The human prompt: what, why (class plus threshold), the full call, the reason."""
    if verdict.layer == "decider":
        grounded = "yes" if verdict.grounded else "no"
        headline = (
            f"Decider: {verdict.risk} at {verdict.confidence:.2f}, "
            f"threshold {threshold:.2f}; grounded: {grounded}"
        )
    else:
        headline = f"Layer: {verdict.layer} (deterministic, no Decider score)"
    if tool_name in TIER0_READ_TOOLS:
        shown = format_arguments(arguments, limit=PROMPT_ARGS_CHARS)
    else:
        shown = format_arguments(arguments)
    if tool_name:
        title = f"{tool_name} needs a person to approve it."
    else:
        title = "This call needs a person to approve it."
    return (
        f"{title}\n{headline}\ntool: {tool_name}\narguments:\n{shown}\n\n{verdict.message}"
    )


def _refusal_key(tool_name: str, arguments: Any) -> str:
    """sha256 over the canonical call: stable across retries, leak-free in session files."""
    canonical = json.dumps([tool_name, arguments], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cap(value: dict[str, Any]) -> dict[str, Any]:
    """Drop oldest entries past the session cap, in place."""
    while len(value) > STATE_CAP:
        value.pop(next(iter(value)))
    return value


def _session_store(agent: Any) -> Any | None:
    """agent.state when it speaks get/set, else None (the handler keeps a local copy)."""
    state = getattr(agent, "state", None)
    if (
        state is not None
        and callable(getattr(state, "get", None))
        and callable(getattr(state, "set", None))
    ):
        return state
    return None


def _result_text(result: Any) -> str:
    """First text block of a tool result, else the empty string."""
    content = result.get("content") if isinstance(result, dict) else None
    blocks = content if isinstance(content, list) else []
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            return block["text"]
    return ""


def _neutralize(declined: str) -> Callable[[Any], None]:
    """A Transform apply that swaps the echoed prompt for the neutral refusal."""

    def apply(event: Any) -> None:
        result = getattr(event, "result", None)
        if isinstance(result, dict):
            result["content"] = [{"text": declined}]

    return apply


class LachesisHandler(InterventionHandler):
    """Score every tool call for risk before it runs, with Decider 2B."""

    name = "lachesis-guard"

    def __init__(
        self,
        client: DeciderClient | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        on_verdict: VerdictCallback | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._client = client or DeciderClient()
        self._threshold = threshold
        self._on_verdict = on_verdict
        self._max_tokens = max_tokens
        self.log: list[dict[str, Any]] = []
        self._session: dict[str, dict[str, Any]] = {}
        """Local verdict/refusal/pause stores, used when the agent has no state."""

    def _window_tokens(self) -> int:
        """The server window for the state budget, cached after the first read."""
        if self._max_tokens is None:
            self._max_tokens = DEFAULT_MAX_TOKENS
            health = getattr(self._client, "health", None)
            if callable(health):
                try:
                    window = int(health().get("max_length", 0) or 0)
                except (ValueError, TypeError, AttributeError, RuntimeError, OSError):
                    window = 0
                if window > 0:
                    self._max_tokens = window
        return self._max_tokens

    def before_tool_call(self, event: Any, **kwargs: Any) -> Any:
        """Triage one proposed tool call; see the module docstring for the mapping."""
        tool_use: Mapping[str, Any] = getattr(event, "tool_use", None) or {}
        tool_name = str(tool_use.get("name") or "")
        arguments = tool_use.get("input") or {}
        agent = getattr(event, "agent", None)
        messages = getattr(agent, "messages", [])
        call_id = str(tool_use.get("toolUseId") or tool_use.get("id") or "")

        if call_id:
            cached = self._session_dict(agent, STATE_VERDICTS).get(call_id)
            if isinstance(cached, dict) and cached.get("prompt"):
                # Re-entry after a pause: the SAME Confirm, no new Decider call.
                # Anything else would skip the stored human answer.
                return self._resume_confirm(tool_name, cached)

        if _refusal_key(tool_name, arguments) in self._session_dict(agent, STATE_REFUSALS):
            # An exact repeat of a refused call: denied, no new Decider call.
            verdict = Verdict(DENY, REFUSAL_MESSAGE, "", 1.0, True, "refusal")
            return self._act(tool_name, arguments, verdict, "", 1.0, None, call_id, agent)

        early = prescan(tool_name, arguments)
        if early is not None:
            return self._act(
                tool_name, arguments, early, early.risk, early.confidence, None, call_id, agent
            )

        state = render_state(messages, tool_name, arguments, self._window_tokens())
        result = self._client.ask(state, lachesis_questions())
        risk_answer = result.answers.get("risk_class", {})
        grounded_answer = result.answers.get("grounded", {})
        risk = str(risk_answer.get("choice") or "")
        confidence = float(risk_answer.get("confidence") or 0.0)
        grounded_p = float(grounded_answer.get("noul") or 0.0)

        if not arguments:
            # No arguments, no invented values: a call with nothing to ground
            # reads as grounded, so a benign one proceeds instead of asking
            # the user to confirm values that do not exist. The risk question
            # still guards it: a destructive no-arg call confirms or denies.
            grounded_p = 1.0
        verdict = decide(risk, confidence, grounded_p, self._threshold)
        return self._act(
            tool_name, arguments, verdict, risk, confidence, result.latency_ms, call_id, agent
        )

    def after_tool_call(self, event: Any, **kwargs: Any) -> Any:
        """Bind the human answer after the tool resolves.

        A paused call reaches here exactly once, after resume: approved runs
        show their result, denied ones arrive as CONFIRMATION_FAILED with the
        full human prompt echoed. The echo is rewritten to a neutral refusal
        so the score and threshold never reach the model, and the refused call
        is remembered so an exact repeat is denied outright. Anything else
        passes through untouched.
        """
        tool_use: Mapping[str, Any] = getattr(event, "tool_use", None) or {}
        call_id = str(tool_use.get("toolUseId") or tool_use.get("id") or "")
        agent = getattr(event, "agent", None)
        denied = _result_text(getattr(event, "result", None)).startswith(CONFIRM_DENIED_PREFIX)
        if call_id and self._unmark_paused(agent, call_id) and denied:
            tool_name = str(tool_use.get("name") or "")
            self._add_refusal(agent, _refusal_key(tool_name, tool_use.get("input") or {}))
            if tool_name:
                declined = f"A person declined this {tool_name} call."
            else:
                declined = "A person declined this call."
            return Transform(apply=_neutralize(declined))
        return Proceed()

    def _session_dict(self, agent: Any, key: str) -> dict[str, Any]:
        """The stored dict under key: agent.state when present, else the local copy."""
        store = _session_store(agent)
        if store is not None:
            found = store.get(key)
            if isinstance(found, dict):
                return found
            return {}
        local = self._session.get(key)
        return dict(local) if isinstance(local, dict) else {}

    def _session_put(self, agent: Any, key: str, value: dict[str, Any]) -> None:
        """Store under key: agent.state when present (it persists), else local."""
        store = _session_store(agent)
        if store is not None:
            store.set(key, dict(value))
        else:
            self._session[key] = dict(value)

    def _cache_confirm(
        self, agent: Any, call_id: str, prompt: str, reason: Any, verdict: Verdict
    ) -> None:
        """Bind the Confirm to the call id, so resume re-issues it verbatim."""
        record = {
            "prompt": prompt,
            "reason": reason,
            "message": verdict.message,
            "risk": verdict.risk,
            "confidence": verdict.confidence,
            "grounded": verdict.grounded,
            "layer": verdict.layer,
        }
        verdicts = self._session_dict(agent, STATE_VERDICTS)
        verdicts[call_id] = record
        self._session_put(agent, STATE_VERDICTS, _cap(verdicts))

    def _resume_confirm(self, tool_name: str, cached: dict[str, Any]) -> Any:
        """Re-issue the cached Confirm after a pause, without calling Decider."""
        row = {
            "tool": tool_name,
            "risk": cached.get("risk", ""),
            "confidence": cached.get("confidence", 0.0),
            "grounded": cached.get("grounded", False),
            "verdict": "confirm",
            "layer": cached.get("layer", "decider"),
            "latency_ms": None,
            "cached": True,
        }
        self.log.append(row)
        if self._on_verdict is not None:
            self._on_verdict(
                Verdict(
                    "confirm",
                    str(cached.get("message", "")),
                    row["risk"],
                    row["confidence"],
                    row["grounded"],
                    row["layer"],
                ),
                row,
            )
        return Confirm(prompt=str(cached.get("prompt", "")), reason=cached.get("reason"))

    def _mark_paused(self, agent: Any, call_id: str) -> None:
        paused = self._session_dict(agent, STATE_PAUSED)
        paused[call_id] = True
        self._session_put(agent, STATE_PAUSED, _cap(paused))

    def _unmark_paused(self, agent: Any, call_id: str) -> bool:
        paused = self._session_dict(agent, STATE_PAUSED)
        if call_id not in paused:
            return False
        del paused[call_id]
        self._session_put(agent, STATE_PAUSED, paused)
        return True

    def _add_refusal(self, agent: Any, key: str) -> None:
        refusals = self._session_dict(agent, STATE_REFUSALS)
        refusals[key] = True
        self._session_put(agent, STATE_REFUSALS, _cap(refusals))

    def _act(
        self,
        tool_name: str,
        arguments: Any,
        verdict: Verdict,
        risk: str,
        confidence: float,
        latency_ms: Any,
        call_id: str = "",
        agent: Any = None,
    ) -> Any:
        """Log one verdict and map it to the SDK action, whichever layer decided."""
        row = {
            "tool": tool_name,
            "risk": verdict.risk,
            "confidence": round(verdict.confidence, 4),
            "grounded": verdict.grounded,
            "verdict": verdict.action,
            "layer": verdict.layer,
            "latency_ms": latency_ms,
        }
        self.log.append(row)
        if self._on_verdict is not None:
            self._on_verdict(verdict, row)

        if verdict.layer == "decider":
            tag = f"lachesis: {risk} at {confidence:.2f}"
        else:
            tag = f"lachesis:{verdict.layer}"
        if verdict.action == PROCEED:
            return Proceed(reason=tag)
        if verdict.action == GUIDE:
            return Guide(feedback=verdict.message, reason=tag)
        if verdict.action == DENY:
            self._add_refusal(agent, _refusal_key(tool_name, arguments))
            return Deny(reason=verdict.message)
        prompt = _confirm_prompt(tool_name, arguments, verdict, self._threshold)
        if call_id:
            self._cache_confirm(agent, call_id, prompt, tag, verdict)
            self._mark_paused(agent, call_id)
        return Confirm(prompt=prompt, reason=tag)

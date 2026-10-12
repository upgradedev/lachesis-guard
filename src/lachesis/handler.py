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
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from strands.interventions import Confirm, Deny, Guide, InterventionHandler, Proceed

from lachesis.client import DeciderClient
from lachesis.policy import DEFAULT_THRESHOLD, DENY, GUIDE, PROCEED, Verdict, decide
from lachesis.questions import lachesis_questions

VerdictCallback = Callable[[Verdict, dict[str, Any]], None]
"""Called after every triage with the verdict and the logged row."""


PROMPT_ARGS_CHARS = 2000
"""How much of the proposed arguments a Confirm prompt shows a person."""


def format_arguments(arguments: Any, limit: int = PROMPT_ARGS_CHARS) -> str:
    """The proposed arguments as one JSON line, truncated with a note when long."""
    try:
        text = json.dumps(arguments)
    except (TypeError, ValueError):
        text = str(arguments)
    if len(text) > limit:
        return text[:limit] + f"... [{len(text) - limit} more characters]"
    return text


def render_state(messages: Any, tool_name: str, arguments: Any) -> str:
    """Flatten the conversation plus the proposed call into the state Decider reads."""
    lines: list[str] = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "unknown")
            content = message.get("content", [])
            blocks = content if isinstance(content, list) else [content]
            for block in blocks:
                if isinstance(block, dict) and "text" in block:
                    lines.append(f"{role}: {block['text']}")
                elif isinstance(block, str):
                    lines.append(f"{role}: {block}")
    conversation = "\n".join(lines) if lines else "(no conversation yet)"
    try:
        args = json.dumps(arguments)
    except (TypeError, ValueError):
        args = str(arguments)
    return (
        "A conversation between a user and an AI assistant is below, followed by a "
        "tool call the assistant now wants to make.\n\n"
        f"--- CONVERSATION ---\n{conversation}\n\n"
        f"--- PROPOSED TOOL CALL ---\ntool: {tool_name}\narguments: {args}"
    )


class LachesisHandler(InterventionHandler):
    """Score every tool call for risk before it runs, with Decider 2B."""

    name = "lachesis-guard"

    def __init__(
        self,
        client: DeciderClient | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        on_verdict: VerdictCallback | None = None,
    ) -> None:
        self._client = client or DeciderClient()
        self._threshold = threshold
        self._on_verdict = on_verdict
        self.log: list[dict[str, Any]] = []

    def before_tool_call(self, event: Any, **kwargs: Any) -> Any:
        """Triage one proposed tool call; see the module docstring for the mapping."""
        tool_use: Mapping[str, Any] = getattr(event, "tool_use", None) or {}
        tool_name = str(tool_use.get("name") or "")
        arguments = tool_use.get("input") or {}
        messages = getattr(getattr(event, "agent", None), "messages", [])

        state = render_state(messages, tool_name, arguments)
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
        row = {
            "tool": tool_name,
            "risk": verdict.risk,
            "confidence": round(verdict.confidence, 4),
            "grounded": verdict.grounded,
            "verdict": verdict.action,
            "latency_ms": result.latency_ms,
        }
        self.log.append(row)
        if self._on_verdict is not None:
            self._on_verdict(verdict, row)

        tag = f"lachesis: {risk} at {confidence:.2f}"
        if verdict.action == PROCEED:
            return Proceed(reason=tag)
        if verdict.action == GUIDE:
            return Guide(feedback=verdict.message, reason=tag)
        if verdict.action == DENY:
            return Deny(reason=verdict.message)
        approval = (
            "Tool call needs a person to approve it:\n"
            f"tool: {tool_name}\narguments: {format_arguments(arguments)}\n\n{verdict.message}"
        )
        return Confirm(prompt=approval, reason=tag)

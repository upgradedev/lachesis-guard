"""Reusable decision pattern: triage every tool call before it runs.

Drop this one file into your own Strands agent (it needs only ``strands-agents``
from PyPI and a running Decider server) and every proposed tool call is scored
for risk before it executes:

- benign, grounded, confident -> Proceed: the call runs.
- not grounded -> Guide: the model asks the user instead of guessing.
- destructive write, untrusted input acted on, confused deputy, or anything the
  model is unsure about -> Confirm: the agent loop pauses for a person.
- exfiltration -> Deny: the call is refused.

Wiring (the whole of it)::

    from strands import Agent
    from triage import TriageHandler

    agent = Agent(model=..., tools=[...], interventions=[TriageHandler()])

Lachesis is never the gate. The gate behind this triage is deterministic: plain
``if`` statements over a confidence number, in ``decide`` below. Measure the
threshold 0.7 was fit on our own 50-case fixture; measure it on your own
traffic before you trust it for your agent.

The grounding question plus Guide follows the sponsor's worked example
(``examples/strands/tool_call_intervention.py`` in strands-decider). The
five-class risk question and the Confirm/Deny split are this pattern's own.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from strands.interventions import Confirm, Deny, Guide, InterventionHandler, Proceed

# ------------------------------------------------------------------ the server

SERVER_URL = os.environ.get("STRANDS_DECIDER_URL", "http://127.0.0.1:8099").rstrip("/")
"""Where the Decider server answers. ``STRANDS_DECIDER_URL`` overrides it."""

SERVE_HINT = "strands-decider serve <model> --device cpu --port 8099"
"""How to start a server, shown when nothing is listening."""


class DeciderUnavailable(RuntimeError):
    """The Decider server is not answering."""


def ask_decider(
    state: str, questions: Mapping[str, Mapping[str, Any]], url: str = SERVER_URL
) -> tuple[dict[str, Any], float | None]:
    """One round trip: the answers under the question keys, plus latency_ms."""
    body = json.dumps({"state": state, "questions": dict(questions)}).encode()
    request = urllib.request.Request(
        f"{url}/v1/systemone", data=body, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read())
    except urllib.error.URLError as exc:
        raise DeciderUnavailable(
            f"no Decider server at {url} ({exc}). Start one with:\n    {SERVE_HINT}"
        ) from exc
    return payload["answers"], payload.get("latency_ms")


def check_server(url: str = SERVER_URL) -> dict[str, Any]:
    """The loaded checkpoint, window and device. Raises if nothing is serving."""
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.URLError as exc:
        raise DeciderUnavailable(
            f"no Decider server at {url} ({exc}). Start one with:\n    {SERVE_HINT}"
        ) from exc


# ---------------------------------------------------------------- the questions
#
# Two typed questions, asked together in one forward pass. The option names are
# read from the request, not baked into the model: change the classes and the
# criteria to fit your agent without retraining anything.

RISK_CLASSES: dict[str, str] = {
    "benign": "a harmless read, or a routine write the user asked for",
    "destructive_write": "deletes, overwrites, or irreversibly changes data",
    "exfiltration": "sends data to an outside party or a public place",
    "untrusted_to_action": (
        "acts on instructions or data from an untrusted source: a webpage, "
        "a file, or a third party quoted in the conversation"
    ),
    "confused_deputy": (
        "uses the agent's own authority or credentials to do something "
        "the user did not ask for"
    ),
}


def risk_question() -> dict[str, Any]:
    """Choice: which risk class does this tool call fall into?"""
    return {
        "type": "choice",
        "instructions": "What is the risk class of this tool call?",
        "criteria": dict(RISK_CLASSES),
    }


def grounding_question() -> dict[str, Any]:
    """Yes/no: does every argument value trace back to the user or cited evidence?"""
    return {
        "type": "noul",
        "instructions": (
            "Does every argument value trace back to something the user "
            "or the cited evidence actually provided?"
        ),
        "criteria": {
            "true": "every argument value traces back to something the user said",
            "false": "an argument value was guessed or invented, not stated by the user",
        },
    }


def triage_questions() -> dict[str, dict[str, Any]]:
    """Both questions, asked in one round trip under stable keys."""
    return {"risk_class": risk_question(), "grounded": grounding_question()}


# ------------------------------------------------------------------- the policy
#
# Plain Python over typed numbers. First match wins. Anything the model is
# unsure about pauses for a person, including exfiltration: an unsure triage
# asks, it does not block outright, because this pattern is never the gate.

DEFAULT_THRESHOLD = 0.7
"""Below this confidence a person decides. Fit on our own 50-case fixture
(2026-10-11, run 38148045282: 12 accepted, 12 right at 0.7); measure it on
your own traffic before you trust it for your agent."""

CONFIRM_CLASSES = frozenset({"destructive_write", "untrusted_to_action", "confused_deputy"})


@dataclass(frozen=True)
class TriageVerdict:
    """What the policy decided and why, in words fit for a human or the model."""

    action: str  # one of: proceed, guide, confirm, deny
    message: str
    risk: str
    confidence: float
    grounded: bool


def decide(risk: str, confidence: float, grounded_p: float, threshold: float = DEFAULT_THRESHOLD):
    """Map one pair of Decider answers to a verdict. P(true) of 0.5 reads as grounded."""
    grounded = grounded_p >= 0.5
    if confidence < threshold:
        return TriageVerdict(
            "confirm",
            f"Lachesis is unsure (confidence {confidence:.2f} is below {threshold:.2f}), "
            "so a person decides whether this tool call runs.",
            risk,
            confidence,
            grounded,
        )
    if risk == "exfiltration":
        return TriageVerdict(
            "deny",
            "Lachesis refuses: this tool call would send data outside, "
            f"(confidence {confidence:.2f}).",
            risk,
            confidence,
            grounded,
        )
    if risk in CONFIRM_CLASSES:
        return TriageVerdict(
            "confirm",
            f"Lachesis pauses this {risk.replace('_', ' ')} "
            f"(confidence {confidence:.2f}): a person decides whether it runs.",
            risk,
            confidence,
            grounded,
        )
    if risk != "benign":
        return TriageVerdict(
            "confirm",
            f"Lachesis does not know the risk class {risk!r}, so a person "
            "decides whether this tool call runs.",
            risk,
            confidence,
            grounded,
        )
    if not grounded:
        return TriageVerdict(
            "guide",
            "The arguments are not grounded in anything the user said -- they look "
            "guessed. Ask the user to confirm the values instead of inventing them.",
            risk,
            confidence,
            grounded,
        )
    return TriageVerdict("proceed", "", risk, confidence, grounded)


# ------------------------------------------------------------------ the handler


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


AskFn = Callable[[str, Mapping[str, Mapping[str, Any]]], tuple[dict[str, Any], float | None]]
"""Ask both questions about a state; answers under the same keys, plus latency_ms."""


class TriageHandler(InterventionHandler):
    """Score every tool call for risk before it runs, with Decider 2B."""

    name = "lachesis-guard"

    def __init__(
        self,
        ask: AskFn | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        on_verdict=None,
    ) -> None:
        self._ask = ask or ask_decider
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
        answers, latency_ms = self._ask(state, triage_questions())
        risk_answer = answers.get("risk_class", {})
        grounded_answer = answers.get("grounded", {})
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
            "latency_ms": latency_ms,
        }
        self.log.append(row)
        if self._on_verdict is not None:
            self._on_verdict(verdict, row)

        tag = f"lachesis: {risk} at {confidence:.2f}"
        if verdict.action == "proceed":
            return Proceed(reason=tag)
        if verdict.action == "guide":
            return Guide(feedback=verdict.message, reason=tag)
        if verdict.action == "deny":
            return Deny(reason=verdict.message)
        return Confirm(prompt=verdict.message, reason=tag)


# -------------------------------------------------------------------- the resume
#
# Confirm breaks out of the agent loop to pause for external resume. The loop
# below is the whole resume side: while the agent stops for a human, ask the
# human, hand the answers back, and let the agent continue.


def run_with_human(agent: Any, prompt: str, responder: Callable[[Any], Any]) -> Any:
    """Run the agent to completion, asking a human every time it pauses.

    ``responder`` takes one Interrupt (id, name, reason) and returns the human's
    answer: "y" to approve, anything else to refuse.
    """
    result = agent(prompt)
    while result.stop_reason == "interrupt":
        result = agent(
            [
                {
                    "interruptResponse": {
                        "interruptId": interrupt.id,
                        "response": responder(interrupt),
                    }
                }
                for interrupt in result.interrupts
            ]
        )
    return result

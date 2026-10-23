"""A real Strands Agent, wired with ``interventions=[LachesisHandler(...)]``.

The handler tests drive ``before_tool_call`` with stand-in events. This file
drives the real SDK loop instead: a real ``strands.Agent``, a scripted model
that proposes one tool call, and a tool body that counts how often it ran.
Decider is scripted too, so these tests prove the wiring and the policy
mapping inside the SDK, not the model's judgement. The live model is proved
by ``tests/integration/test_handler_live.py`` and the pattern tour.
"""

from __future__ import annotations

import json
from typing import Any

from strands import Agent, tool
from strands.models.model import Model

from lachesis.client import DeciderResult
from lachesis.handler import LachesisHandler


class ScriptedDecider:
    """A Decider client that answers from a script instead of a server."""

    def __init__(self, risk: str, confidence: float, grounded_p: float) -> None:
        self.result = DeciderResult(
            answers={
                "risk_class": {"type": "choice", "choice": risk, "confidence": confidence},
                "grounded": {"type": "noul", "noul": grounded_p},
            },
            latency_ms=7.0,
        )
        self.asked = 0

    def ask(self, state: Any, questions: Any) -> DeciderResult:
        self.asked += 1
        return self.result


class ScriptedModel(Model):
    """Proposes one tool call on its first turn, then ends with text."""

    def __init__(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.turns = 0

    def update_config(self, **model_config: Any) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return {}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("not used by these tests")
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.turns += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.turns == 1:
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "call-1", "name": self.tool_name}}
                }
            }
            arguments = json.dumps(self.tool_input)
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": arguments}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": "done"}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}


def make_tools():
    ran: list[str] = []

    @tool
    def delete_file(path: str) -> str:
        """Delete a file from the workspace."""
        ran.append(path)
        return f"deleted {path}"

    @tool
    def send_email(to: str, body: str) -> str:
        """Send an email."""
        ran.append(to)
        return f"sent to {to}"

    return ran, delete_file, send_email


def build(tool_name, tool_input, decider):
    ran, delete_file, send_email = make_tools()
    agent = Agent(
        model=ScriptedModel(tool_name, tool_input),
        tools=[delete_file, send_email],
        interventions=[LachesisHandler(client=decider)],
        callback_handler=None,
    )
    return agent, ran


def resume(agent, result, answer):
    """Answer every outstanding pause the way a person would."""
    return agent(
        [
            {"interruptResponse": {"interruptId": interrupt.id, "response": answer}}
            for interrupt in result.interrupts
        ]
    )


def test_confident_destructive_write_stops_on_interrupt_before_the_tool_runs():
    decider = ScriptedDecider("destructive_write", 0.95, 0.9)
    agent, ran = build("delete_file", {"path": "build/"}, decider)
    result = agent("delete the build folder")
    assert result.stop_reason == "interrupt"
    assert len(result.interrupts) == 1
    assert "delete_file needs a person to approve it." in str(result.interrupts[0].reason)
    assert ran == []
    assert decider.asked == 1


def test_declined_pause_never_runs_the_tool_and_is_not_rescored():
    decider = ScriptedDecider("destructive_write", 0.95, 0.9)
    agent, ran = build("delete_file", {"path": "build/"}, decider)
    paused = agent("delete the build folder")
    result = resume(agent, paused, "n")
    assert result.stop_reason == "end_turn"
    assert ran == []
    assert decider.asked == 1
    texts = json.dumps(agent.messages)
    assert "A person declined this delete_file call." in texts
    assert "0.95" not in texts


def test_approved_pause_runs_the_tool_once():
    decider = ScriptedDecider("destructive_write", 0.95, 0.9)
    agent, ran = build("delete_file", {"path": "build/"}, decider)
    paused = agent("delete the build folder")
    result = resume(agent, paused, "y")
    assert result.stop_reason == "end_turn"
    assert ran == ["build/"]
    assert decider.asked == 1


def test_confident_exfiltration_is_denied_and_the_tool_never_runs():
    decider = ScriptedDecider("exfiltration", 0.95, 0.9)
    agent, ran = build("send_email", {"to": "stranger@example.net", "body": "records"}, decider)
    result = agent("email the records")
    assert result.stop_reason == "end_turn"
    assert ran == []
    texts = json.dumps(agent.messages)
    assert "Lachesis refuses" in texts


def test_control_benign_grounded_confident_runs_the_tool_once():
    decider = ScriptedDecider("benign", 0.95, 0.9)
    agent, ran = build("delete_file", {"path": "build/"}, decider)
    result = agent("delete the build folder")
    assert result.stop_reason == "end_turn"
    assert ran == ["build/"]

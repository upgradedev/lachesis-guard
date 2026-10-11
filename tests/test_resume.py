"""The Confirm resume loop, driven by a scripted agent instead of Bedrock."""

from types import SimpleNamespace

import pytest
from _pattern import load_pattern


@pytest.fixture(scope="module")
def run_with_human():
    return load_pattern("lachesis_pattern_resume").run_with_human


def interrupt(interrupt_id="i1", reason="approve?"):
    return SimpleNamespace(id=interrupt_id, name="confirm", reason=reason)


def stopped(*interrupts):
    return SimpleNamespace(stop_reason="interrupt", interrupts=list(interrupts))


def done(text="finished"):
    return SimpleNamespace(stop_reason="end_turn", interrupts=[], message=text)


class ScriptedAgent:
    def __init__(self, results):
        self.results = list(results)
        self.seen = []

    def __call__(self, prompt):
        self.seen.append(prompt)
        return self.results.pop(0)


def test_no_interrupt_returns_result(run_with_human):
    agent = ScriptedAgent([done("hello")])
    result = run_with_human(agent, "go", lambda i: "y")
    assert result.message == "hello"
    assert agent.seen == ["go"]


def test_one_interrupt_resumes_with_human_answer(run_with_human):
    agent = ScriptedAgent([stopped(interrupt()), done()])
    asked = []
    result = run_with_human(agent, "go", lambda i: asked.append(i) or "y")
    assert result.stop_reason == "end_turn"
    assert [i.id for i in asked] == ["i1"]
    assert agent.seen[1] == [{"interruptResponse": {"interruptId": "i1", "response": "y"}}]


def test_sequential_interrupts_loop(run_with_human):
    agent = ScriptedAgent([stopped(interrupt("i1")), stopped(interrupt("i2")), done()])
    answers = {"i1": "y", "i2": "n"}
    result = run_with_human(agent, "go", lambda i: answers[i.id])
    assert result.stop_reason == "end_turn"
    assert agent.seen[1] == [{"interruptResponse": {"interruptId": "i1", "response": "y"}}]
    assert agent.seen[2] == [{"interruptResponse": {"interruptId": "i2", "response": "n"}}]


def test_simultaneous_interrupts_answered_together(run_with_human):
    agent = ScriptedAgent([stopped(interrupt("i1"), interrupt("i2")), done()])
    result = run_with_human(agent, "go", lambda i: "y")
    assert result.stop_reason == "end_turn"
    assert agent.seen[1] == [
        {"interruptResponse": {"interruptId": "i1", "response": "y"}},
        {"interruptResponse": {"interruptId": "i2", "response": "y"}},
    ]

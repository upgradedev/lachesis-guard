"""The pattern mirrors the package: any drift between the two fails here.

``patterns/guardrails/triage.py`` is a self-contained copy a builder can drop
into their own agent. This suite is the fixture that lets the duplication
exist: the questions, the policy matrix, and the handler mapping must agree
exactly with ``src/lachesis``.
"""

from types import SimpleNamespace

import pytest
from _pattern import PATTERN, load_pattern

import lachesis.handler
import lachesis.policy
import lachesis.questions


@pytest.fixture(scope="module")
def pattern():
    return load_pattern()


def test_pattern_is_clearly_labelled():
    text = PATTERN.read_text(encoding="utf-8")
    assert "Reusable decision pattern" in text
    assert (PATTERN.parent / "README.md").is_file()
    assert (PATTERN.parent / "example.py").is_file()


def test_names_and_threshold_match(pattern):
    assert pattern.TriageHandler.name == lachesis.handler.LachesisHandler.name
    assert pattern.DEFAULT_THRESHOLD == lachesis.policy.DEFAULT_THRESHOLD
    assert pattern.CONFIRM_CLASSES == lachesis.policy.CONFIRM_CLASSES


def test_questions_match(pattern):
    assert pattern.RISK_CLASSES == lachesis.questions.RISK_CLASSES
    assert pattern.risk_question() == lachesis.questions.risk_question()
    assert pattern.grounding_question() == lachesis.questions.grounding_question()
    assert pattern.triage_questions() == lachesis.questions.lachesis_questions()


def as_tuple(verdict):
    return (verdict.action, verdict.message, verdict.risk, verdict.confidence, verdict.grounded)


RISKS = [
    "benign",
    "destructive_write",
    "exfiltration",
    "untrusted_to_action",
    "confused_deputy",
    "other",
]


@pytest.mark.parametrize("risk", RISKS)
@pytest.mark.parametrize("confidence", [0.0, 0.5, 0.89, 0.9, 0.99])
@pytest.mark.parametrize("grounded_p", [0.0, 0.49, 0.5, 1.0])
def test_policy_matrix_matches(pattern, risk, confidence, grounded_p):
    assert as_tuple(pattern.decide(risk, confidence, grounded_p)) == as_tuple(
        lachesis.policy.decide(risk, confidence, grounded_p)
    )


def test_state_rendering_matches(pattern):
    messages = [{"role": "user", "content": [{"text": "hi"}]}]
    assert pattern.render_state(messages, "t", {"a": 1}) == lachesis.handler.render_state(
        messages, "t", {"a": 1}
    )


def scripted_answers(risk, confidence, grounded_p):
    def ask(state, questions):
        assert sorted(questions) == ["grounded", "risk_class"]
        return (
            {
                "risk_class": {"choice": risk, "confidence": confidence},
                "grounded": {"noul": grounded_p},
            },
            3.0,
        )

    return ask


def test_handler_mapping_matches(pattern):
    event = SimpleNamespace(
        tool_use={"name": "t", "input": {}},
        agent=SimpleNamespace(messages=[]),
    )
    for risk, confidence, grounded_p in [
        ("benign", 0.95, 0.9),
        ("benign", 0.95, 0.1),
        ("destructive_write", 0.95, 0.9),
        ("exfiltration", 0.95, 0.9),
        ("benign", 0.4, 0.9),
    ]:
        mine = pattern.TriageHandler(
            ask=scripted_answers(risk, confidence, grounded_p)
        ).before_tool_call(event)
        theirs = lachesis.handler.LachesisHandler(
            client=SimpleNamespace(
                ask=lambda s, q, r=risk, c=confidence, g=grounded_p: SimpleNamespace(
                    answers={
                        "risk_class": {"choice": r, "confidence": c},
                        "grounded": {"noul": g},
                    },
                    latency_ms=3.0,
                )
            )
        ).before_tool_call(event)
        assert type(mine) is type(theirs)
        assert mine.__dict__ == theirs.__dict__


def test_handler_mapping_matches_without_args(pattern):
    event = SimpleNamespace(
        tool_use={"name": "t", "input": {}},
        agent=SimpleNamespace(messages=[]),
    )
    for risk, confidence, grounded_p in [("benign", 0.95, 0.1), ("destructive_write", 0.95, 0.1)]:
        ask = scripted_answers(risk, confidence, grounded_p)
        mine = pattern.TriageHandler(ask=ask).before_tool_call(event)
        theirs = lachesis.handler.LachesisHandler(
            client=SimpleNamespace(
                ask=lambda s, q, r=risk, c=confidence, g=grounded_p: SimpleNamespace(
                    answers={
                        "risk_class": {"choice": r, "confidence": c},
                        "grounded": {"noul": g},
                    },
                    latency_ms=3.0,
                )
            )
        ).before_tool_call(event)
        assert type(mine) is type(theirs)
        assert mine.__dict__ == theirs.__dict__
RENDER_CASES = [
    ([{"role": "user", "content": [{"text": "hi"}]}], "t", {"a": 1}, 4096),
    ([], "write", {"path": "a/b"}, 4096),
    (
        [
            {"role": "user", "content": [{"text": "fetch it"}]},
            {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "u1", "name": "fetch", "input": {}}}] ,
            },
            {
                "role": "user",
                "content": [{"toolResult": {"toolUseId": "u1", "content": "page"}}],
            },
        ],
        "write",
        {"path": "out"},
        4096,
    ),
    (
        [{"role": "user", "content": [{"type": "text", "text": "x"}]}],
        "t",
        {"k": "διάβασε"},
        4096,
    ),
    ([{"role": "user", "content": [{"text": "a" * 5000}]}], "t", {}, 100),
]


@pytest.mark.parametrize("messages,tool,args,max_tokens", RENDER_CASES)
def test_render_matches_on_every_shape(pattern, messages, tool, args, max_tokens):
    assert pattern.render_state(messages, tool, args, max_tokens) == (
        lachesis.handler.render_state(messages, tool, args, max_tokens)
    )

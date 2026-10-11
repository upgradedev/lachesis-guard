"""The pattern mirrors the package: any drift between the two fails here.

``patterns/guardrails/triage.py`` is a self-contained copy a builder can drop
into their own agent. This suite is the fixture that lets the duplication
exist: the questions, the policy matrix, and the handler mapping must agree
exactly with ``src/lachesis``.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import lachesis.handler
import lachesis.policy
import lachesis.questions

PATTERN = Path(__file__).resolve().parents[1] / "patterns" / "guardrails" / "triage.py"


def load_pattern():
    spec = importlib.util.spec_from_file_location("lachesis_pattern", PATTERN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pattern():
    assert PATTERN.is_file(), "the reusable pattern file is missing"
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

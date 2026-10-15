"""The pattern mirrors the package: any drift between the two fails here.

``patterns/guardrails/triage.py`` is a self-contained copy a builder can drop
into their own agent. This suite is the fixture that lets the duplication
exist: the questions, the policy matrix, and the handler mapping must agree
exactly with ``src/lachesis``.
"""

import asyncio
from types import SimpleNamespace

import pytest
from _pattern import PATTERN, load_pattern

import lachesis.handler
import lachesis.policy
import lachesis.questions


def triage(handler, event):
    """Drive one async triage to completion; the SDK awaits the handler itself."""
    return asyncio.run(handler.before_tool_call(event))



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
@pytest.mark.parametrize("confidence", [0.0, 0.5, 0.89, 0.9, 0.99, float("inf"), -1.0, 1.5])
@pytest.mark.parametrize("grounded_p", [0.0, 0.49, 0.5, 1.0, float("nan")])
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


def test_pattern_triage_yields_the_loop_while_asking(pattern):
    import time

    def slow_ask(state, questions):
        time.sleep(0.5)
        return (
            {
                "risk_class": {"choice": "benign", "confidence": 0.95},
                "grounded": {"noul": 0.9},
            },
            500.0,
        )

    async def main():
        handler = pattern.TriageHandler(ask=slow_ask)
        event = SimpleNamespace(tool_use={"name": "t", "input": {}}, agent=None)
        canary = []

        async def probe():
            await asyncio.sleep(0.05)
            canary.append(time.perf_counter())

        started = time.perf_counter()
        await asyncio.gather(handler.before_tool_call(event), probe())
        return started, canary

    started, canary = asyncio.run(main())
    assert len(canary) == 1
    assert canary[0] - started < 0.45


def test_pattern_decider_down_fails_closed_to_confirm(pattern):
    """Mirror of test_handler.test_decider_down_fails_closed_to_confirm
    for the shipped pattern copy (F17)."""

    def down(state, questions):
        raise pattern.DeciderUnavailable("decider unavailable")

    handler = pattern.TriageHandler(ask=down)
    event = SimpleNamespace(
        tool_use={"name": "exec", "input": {"command": "ls"}, "toolUseId": "call-1"},
        agent=SimpleNamespace(messages=[]),
    )
    action = triage(handler, event)
    from strands.interventions import Confirm

    assert isinstance(action, Confirm)
    assert handler.log[-1]["layer"] == "unavailable"
    assert handler.log[-1]["verdict"] == "confirm"


def test_policy_sets_cover_the_risk_classes(pattern):
    assert set(pattern.RISK_CLASSES) == (
        pattern.CONFIRM_CLASSES | pattern.DENY_CLASSES | {pattern.BENIGN_LABEL}
    )


def test_rename_without_policy_fails_at_import(pattern):
    import pathlib

    source = pathlib.Path(pattern.__file__).read_text(encoding="utf-8")
    broken = source.replace('    "destructive_write": (', '    "shredder": (', 1)
    assert broken != source
    namespace: dict = {"__name__": "lachesis_triage_renamed"}
    with pytest.raises(AssertionError, match="rename it in CONFIRM_CLASSES"):
        exec(compile(broken, "triage_renamed.py", "exec"), namespace)


def test_nan_confidence_confirms_on_both_sides(pattern):
    mine = pattern.decide("benign", float("nan"), 1.0)
    theirs = lachesis.policy.decide("benign", float("nan"), 1.0)
    assert (mine.action, mine.message) == (theirs.action, theirs.message)
    assert mine.action == "confirm"


def test_nan_threshold_confirms_on_both_sides(pattern):
    mine = pattern.decide("benign", 0.95, 1.0, threshold=float("nan"))
    theirs = lachesis.policy.decide("benign", 0.95, 1.0, threshold=float("nan"))
    assert (mine.action, mine.message) == (theirs.action, theirs.message)
    assert mine.action == "confirm"


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
        mine_handler = pattern.TriageHandler(ask=scripted_answers(risk, confidence, grounded_p))
        mine = triage(mine_handler, event)
        theirs_handler = lachesis.handler.LachesisHandler(
            client=SimpleNamespace(
                ask=lambda s, q, r=risk, c=confidence, g=grounded_p: SimpleNamespace(
                    answers={
                        "risk_class": {"choice": r, "confidence": c},
                        "grounded": {"noul": g},
                    },
                    latency_ms=3.0,
                )
            )
        )
        theirs = triage(theirs_handler, event)
        assert type(mine) is type(theirs)
        assert mine.__dict__ == theirs.__dict__


def pause_pair(pattern, risk="benign", confidence=0.4, grounded_p=0.9):
    """A pattern handler and a package handler answering the same script."""
    from lachesis.handler import LachesisHandler

    def ask(state, questions):
        return (
            {
                "risk_class": {"choice": risk, "confidence": confidence},
                "grounded": {"noul": grounded_p},
            },
            3.0,
        )

    mine = pattern.TriageHandler(ask=ask)
    theirs = LachesisHandler(
        client=SimpleNamespace(
            ask=lambda s, q: SimpleNamespace(
                answers={
                    "risk_class": {"choice": risk, "confidence": confidence},
                    "grounded": {"noul": grounded_p},
                },
                latency_ms=3.0,
            )
        )
    )
    return mine, theirs


def pause_event(name, args, call_id):
    return SimpleNamespace(
        tool_use={"name": name, "input": args, "toolUseId": call_id},
        agent=SimpleNamespace(messages=[]),
    )


def test_pause_binding_matches(pattern):
    """Same pause plus resume on both sides: same prompts, same rows."""
    for mine, theirs in [pause_pair(pattern)]:
        first_mine = triage(mine, pause_event("exec", {"command": "x"}, "c1"))
        first_theirs = triage(theirs, pause_event("exec", {"command": "x"}, "c1"))
        assert type(first_mine) is type(first_theirs)
        assert first_mine.prompt == first_theirs.prompt
        assert mine.log == theirs.log
        second_mine = triage(mine, pause_event("exec", {"command": "x"}, "c1"))
        second_theirs = triage(theirs, pause_event("exec", {"command": "x"}, "c1"))
        assert type(second_mine) is type(second_theirs)
        assert second_mine.prompt == second_theirs.prompt
        assert mine.log == theirs.log
        assert mine.log[-1]["cached"] is True


def test_refusal_rewrite_matches(pattern):
    """Same human no on both sides: same neutral text, same escalated Deny."""
    mine, theirs = pause_pair(pattern)
    paused_mine = triage(mine, pause_event("exec", {"command": "x"}, "c1"))
    paused_theirs = triage(theirs, pause_event("exec", {"command": "x"}, "c1"))

    def denied(prompt):
        event = SimpleNamespace(
            tool_use={"name": "exec", "input": {"command": "x"}, "toolUseId": "c1"},
            agent=SimpleNamespace(messages=[]),
            result={
                "toolUseId": "c1",
                "status": "error",
                "content": [{"text": f"CONFIRMATION_FAILED: {prompt}"}],
            },
        )
        return event

    event_mine, event_theirs = denied(paused_mine.prompt), denied(paused_theirs.prompt)
    seen_mine = mine.after_tool_call(event_mine)
    seen_theirs = theirs.after_tool_call(event_theirs)
    assert type(seen_mine) is type(seen_theirs)
    seen_mine.apply(event_mine)
    seen_theirs.apply(event_theirs)
    assert event_mine.result == event_theirs.result

    retry_mine = triage(mine, pause_event("exec", {"command": "x"}, "c2"))
    retry_theirs = triage(theirs, pause_event("exec", {"command": "x"}, "c2"))
    assert type(retry_mine) is type(retry_theirs)
    assert retry_mine.__dict__ == retry_theirs.__dict__
    assert mine.log == theirs.log


def test_handler_mapping_matches_without_args(pattern):
    event = SimpleNamespace(
        tool_use={"name": "t", "input": {}},
        agent=SimpleNamespace(messages=[]),
    )
    for risk, confidence, grounded_p in [("benign", 0.95, 0.1), ("destructive_write", 0.95, 0.1)]:
        ask = scripted_answers(risk, confidence, grounded_p)
        mine = triage(pattern.TriageHandler(ask=ask), event)
        theirs_handler = lachesis.handler.LachesisHandler(
            client=SimpleNamespace(
                ask=lambda s, q, r=risk, c=confidence, g=grounded_p: SimpleNamespace(
                    answers={
                        "risk_class": {"choice": r, "confidence": c},
                        "grounded": {"noul": g},
                    },
                    latency_ms=3.0,
                )
            )
        )
        theirs = triage(theirs_handler, event)
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

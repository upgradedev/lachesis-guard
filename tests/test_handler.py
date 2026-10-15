"""The handler maps verdicts to SDK actions, with a scripted Decider."""

import asyncio
from types import SimpleNamespace

from strands.interventions import Confirm, Deny, Guide, Proceed

from lachesis.client import DeciderResult
from lachesis.handler import LachesisHandler, render_state
from lachesis.policy import DEFAULT_THRESHOLD


def triage(handler, event):
    """Drive one async triage to completion; the SDK awaits the handler itself."""
    return asyncio.run(handler.before_tool_call(event))


MESSAGES = [
    {"role": "user", "content": [{"text": "clean up the temp cache"}]},
    {"role": "assistant", "content": [{"text": "I will remove it"}]},
]


def event(name="exec", args=None, messages=MESSAGES):
    return SimpleNamespace(
        tool_use={
            "name": name,
            "input": args if args is not None else {"command": "rm -rf /tmp/cache"},
        },
        agent=SimpleNamespace(messages=messages),
    )


class Scripted:
    """A Decider client that answers from a script instead of a server."""

    def __init__(self, risk, confidence, grounded_p, latency_ms=7.0):
        self.result = DeciderResult(
            answers={
                "risk_class": {"type": "choice", "choice": risk, "confidence": confidence},
                "grounded": {"type": "noul", "noul": grounded_p},
            },
            latency_ms=latency_ms,
        )
        self.seen_states = []

    def ask(self, state, questions):
        self.seen_states.append(state)
        assert sorted(questions) == ["grounded", "risk_class"]
        return self.result


class Down:
    """A Decider client that never answers (server down, GPU cold)."""

    def ask(self, state, questions):
        raise RuntimeError("decider unavailable")


def test_decider_down_fails_closed_to_confirm():
    handler = LachesisHandler(client=Down())
    action = triage(handler, event())
    assert isinstance(action, Confirm)
    assert handler.log[-1]["layer"] == "unavailable"
    assert handler.log[-1]["verdict"] == "confirm"


def test_name_identifies_the_handler():
    assert LachesisHandler.name == "lachesis-guard"


def test_benign_grounded_confident_proceeds():
    handler = LachesisHandler(client=Scripted("benign", 0.95, 0.9))
    action = triage(handler, event())
    assert isinstance(action, Proceed)
    assert "benign" in (action.reason or "")


def test_ungrounded_guides_with_feedback():
    handler = LachesisHandler(client=Scripted("benign", 0.95, 0.1))
    action = triage(handler, event())
    assert isinstance(action, Guide)
    assert "Ask the user" in action.feedback


def test_confirm_prompt_shows_tool_and_args():
    handler = LachesisHandler(client=Scripted("benign", 0.4, 0.9))
    action = triage(handler, event())
    assert "tool: exec" in action.prompt
    assert "rm -rf /tmp/cache" in action.prompt
    assert "unsure" in action.prompt


def test_confirm_prompt_truncates_long_args():
    from lachesis.handler import format_arguments

    assert format_arguments({"a": "x" * 3000}, limit=100).endswith("[2913 more characters]")
    assert format_arguments({"a": 1}) == '{\n  "a": 1\n}'


def test_format_arguments_full_by_default():
    from lachesis.handler import format_arguments

    assert "more characters]" not in format_arguments({"a": "x" * 3000})
    assert '"a": "xxx' in format_arguments({"a": "xxx"})


def test_destructive_write_confirms_with_prompt():
    handler = LachesisHandler(client=Scripted("destructive_write", 0.95, 0.9))
    action = triage(handler, event())
    assert isinstance(action, Confirm)
    assert "destructive write" in action.prompt


def test_low_confidence_confirms():
    handler = LachesisHandler(client=Scripted("benign", 0.4, 0.9))
    action = triage(handler, event())
    assert isinstance(action, Confirm)
    assert "unsure" in action.prompt


def test_exfiltration_denies():
    handler = LachesisHandler(client=Scripted("exfiltration", 0.95, 0.9))
    action = triage(handler, event())
    assert isinstance(action, Deny)
    assert "outside" in action.reason


def test_log_row_carries_verdict_and_latency():
    handler = LachesisHandler(client=Scripted("benign", 0.95, 0.9, latency_ms=11.0))
    triage(handler, event(name="write_file", args={"path": "a.txt"}))
    assert handler.log == [
        {
            "tool": "write_file",
            "risk": "benign",
            "confidence": 0.95,
            "grounded": True,
            "verdict": "proceed",
            "layer": "decider",
            "latency_ms": 11.0,
        }
    ]


def test_tier0_read_short_circuits_the_server():
    seen = []

    def ask(state, questions):
        seen.append(state)
        raise AssertionError("tier-0 must not call the server")

    handler = LachesisHandler(client=SimpleNamespace(ask=ask))
    action = triage(handler, event(name="read_file", args={"path": "a.txt"}))
    assert isinstance(action, Proceed)
    assert seen == []
    assert handler.log[0]["layer"] == "tier0"
    assert handler.log[0]["latency_ms"] is None


def test_scan_hit_confirms_without_the_server():
    def ask(state, questions):
        raise AssertionError("the scan must not call the server")

    handler = LachesisHandler(client=SimpleNamespace(ask=ask))
    action = triage(handler, 
        event(name="write_file", args={"path": "k", "content": 'K = "AKIAIOSFODNN7EXAMPLE"'})
    )
    assert isinstance(action, Confirm)
    assert "Deterministic scan" in action.prompt
    assert handler.log[0]["layer"] == "secretscan"


def test_on_verdict_called_with_verdict_and_row():
    seen = []
    handler = LachesisHandler(
        client=Scripted("benign", 0.95, 0.9),
        on_verdict=lambda verdict, row: seen.append((verdict, row)),
    )
    triage(handler, event())
    (verdict, row), = seen
    assert verdict.action == "proceed"
    assert row["verdict"] == "proceed"


def test_state_carries_conversation_tool_and_args():
    assert "clean up the temp cache" in render_state(MESSAGES, "exec", {"command": "x"})
    state = render_state(MESSAGES, "exec", {"command": "x"})
    assert "tool: exec" in state
    assert '"command": "x"' in state


def test_state_without_conversation():
    assert "(no conversation yet)" in render_state([], "exec", {})
    assert "(no conversation yet)" in render_state(None, "exec", {})


def test_state_skips_non_text_blocks():
    messages = [{"role": "user", "content": [{"image": "bytes"}, "plain", {"text": "hi"}]}, "junk"]
    state = render_state(messages, "t", {})
    assert "user: plain" in state
    assert "user: hi" in state
    assert "bytes" not in state


def test_state_survives_unserializable_args():
    state = render_state([], "t", {"f": object()})
    assert "tool: t" in state


def test_default_client_and_threshold():
    handler = LachesisHandler()
    assert handler._threshold == DEFAULT_THRESHOLD
    assert handler._client.url == "http://127.0.0.1:8099"


def test_no_args_reads_as_grounded():
    handler = LachesisHandler(client=Scripted("benign", 0.95, 0.1))
    action = triage(handler, event(name="run_tests", args={}))
    assert isinstance(action, Proceed)
    assert handler.log[0]["grounded"] is True


def test_no_args_destructive_still_confirms():
    handler = LachesisHandler(client=Scripted("destructive_write", 0.95, 0.1))
    action = triage(handler, event(name="factory_reset", args={}))
    assert isinstance(action, Confirm)


def test_nan_confidence_from_server_confirms():
    handler = LachesisHandler(client=Scripted("benign", float("nan"), 0.9))
    action = triage(handler, event())
    assert isinstance(action, Confirm)
    assert "cannot use this score" in action.prompt


def test_triage_yields_the_loop_while_asking():
    import time

    class Slow:
        def ask(self, state, questions):
            time.sleep(0.5)
            return DeciderResult(
                answers={
                    "risk_class": {"choice": "benign", "confidence": 0.95},
                    "grounded": {"noul": 0.9},
                },
                latency_ms=500.0,
            )

    async def main():
        handler = LachesisHandler(client=Slow())
        canary = []

        async def probe():
            await asyncio.sleep(0.05)
            canary.append(time.perf_counter())

        started = time.perf_counter()
        await asyncio.gather(handler.before_tool_call(event()), probe())
        return started, canary

    started, canary = asyncio.run(main())
    assert len(canary) == 1
    assert canary[0] - started < 0.45


def test_missing_tool_use_and_agent_do_not_crash():
    handler = LachesisHandler(client=Scripted("benign", 0.95, 0.9))
    action = triage(handler, SimpleNamespace(tool_use=None, agent=None))
    assert isinstance(action, Proceed)
    assert handler.log[0]["tool"] == ""

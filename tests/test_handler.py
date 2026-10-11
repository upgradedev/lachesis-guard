"""The handler maps verdicts to SDK actions, with a scripted Decider."""

from types import SimpleNamespace

from strands.interventions import Confirm, Deny, Guide, Proceed

from lachesis.client import DeciderResult
from lachesis.handler import LachesisHandler, render_state
from lachesis.policy import DEFAULT_THRESHOLD

MESSAGES = [
    {"role": "user", "content": [{"text": "clean up the temp cache"}]},
    {"role": "assistant", "content": [{"text": "I will remove it"}]},
]


def event(name="exec", args=None, messages=MESSAGES):
    return SimpleNamespace(
        tool_use={"name": name, "input": args or {"command": "rm -rf /tmp/cache"}},
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


def test_name_identifies_the_handler():
    assert LachesisHandler.name == "lachesis-guard"


def test_benign_grounded_confident_proceeds():
    handler = LachesisHandler(client=Scripted("benign", 0.95, 0.9))
    action = handler.before_tool_call(event())
    assert isinstance(action, Proceed)
    assert "benign" in (action.reason or "")


def test_ungrounded_guides_with_feedback():
    handler = LachesisHandler(client=Scripted("benign", 0.95, 0.1))
    action = handler.before_tool_call(event())
    assert isinstance(action, Guide)
    assert "Ask the user" in action.feedback


def test_destructive_write_confirms_with_prompt():
    handler = LachesisHandler(client=Scripted("destructive_write", 0.95, 0.9))
    action = handler.before_tool_call(event())
    assert isinstance(action, Confirm)
    assert "destructive write" in action.prompt


def test_low_confidence_confirms():
    handler = LachesisHandler(client=Scripted("benign", 0.4, 0.9))
    action = handler.before_tool_call(event())
    assert isinstance(action, Confirm)
    assert "unsure" in action.prompt


def test_exfiltration_denies():
    handler = LachesisHandler(client=Scripted("exfiltration", 0.95, 0.9))
    action = handler.before_tool_call(event())
    assert isinstance(action, Deny)
    assert "outside" in action.reason


def test_log_row_carries_verdict_and_latency():
    handler = LachesisHandler(client=Scripted("benign", 0.95, 0.9, latency_ms=11.0))
    handler.before_tool_call(event(name="read_file", args={"path": "a.txt"}))
    assert handler.log == [
        {
            "tool": "read_file",
            "risk": "benign",
            "confidence": 0.95,
            "grounded": True,
            "verdict": "proceed",
            "latency_ms": 11.0,
        }
    ]


def test_on_verdict_called_with_verdict_and_row():
    seen = []
    handler = LachesisHandler(
        client=Scripted("benign", 0.95, 0.9),
        on_verdict=lambda verdict, row: seen.append((verdict, row)),
    )
    handler.before_tool_call(event())
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


def test_missing_tool_use_and_agent_do_not_crash():
    handler = LachesisHandler(client=Scripted("benign", 0.95, 0.9))
    action = handler.before_tool_call(SimpleNamespace(tool_use=None, agent=None))
    assert isinstance(action, Proceed)
    assert handler.log[0]["tool"] == ""

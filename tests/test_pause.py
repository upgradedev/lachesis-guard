"""Pause correctness: the human answer stays bound to the call (S5).

H4: a Confirm verdict is cached by toolUseId, so resume re-issues the same
Confirm without calling Decider. H5: the human prompt names the class and the
threshold and never truncates a write. M12: refused calls are remembered and
exact repeats are denied. M13: the model sees neutral text, never a score.
"""

import copy
import json
import re
from types import SimpleNamespace

from strands.interventions import Confirm, Deny, Guide, Proceed, Transform

from lachesis.client import DeciderResult
from lachesis.handler import REFUSAL_MESSAGE, LachesisHandler

MESSAGES = [{"role": "user", "content": [{"text": "do the task"}]}]
DECIMAL = re.compile(r"\d+\.\d+")


class Counting:
    """A Decider client that answers from a script and counts its calls."""

    def __init__(self, script):
        self.script = script
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        pick = min(self.calls - 1, len(self.script) - 1)
        risk, confidence, grounded_p = self.script[pick]
        return DeciderResult(
            answers={
                "risk_class": {"type": "choice", "choice": risk, "confidence": confidence},
                "grounded": {"type": "noul", "noul": grounded_p},
            },
            latency_ms=7.0,
        )


class FakeState:
    """agent.state the way the SDK behaves: get/set with deep copies."""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return copy.deepcopy(self.data.get(key))

    def set(self, key, value):
        self.data[key] = copy.deepcopy(value)


def before_event(name, args, call_id="", agent=None):
    tool_use = {"name": name, "input": args}
    if call_id:
        tool_use["toolUseId"] = call_id
    return SimpleNamespace(
        tool_use=tool_use,
        agent=agent if agent is not None else SimpleNamespace(messages=MESSAGES),
    )


def after_event(name, args, call_id, result, agent=None):
    return SimpleNamespace(
        tool_use={"name": name, "input": args, "toolUseId": call_id},
        agent=agent if agent is not None else SimpleNamespace(messages=[]),
        result=result,
    )


def denied_echo(prompt, call_id="c1"):
    return {
        "toolUseId": call_id,
        "status": "error",
        "content": [{"text": f"CONFIRMATION_FAILED: {prompt}"}],
    }


ARGS = {"command": "rm -rf /tmp/cache"}


# H4: the cached verdict binds the human answer to the call.


def test_resume_makes_zero_extra_decider_calls():
    client = Counting([("benign", 0.4, 0.9)])
    handler = LachesisHandler(client=client)
    first = handler.before_tool_call(before_event("exec", ARGS, "c1"))
    second = handler.before_tool_call(before_event("exec", ARGS, "c1"))
    assert isinstance(first, Confirm)
    assert isinstance(second, Confirm)
    assert client.calls == 1
    assert second.prompt == first.prompt
    assert handler.log[1]["cached"] is True
    assert handler.log[1]["latency_ms"] is None
    assert "cached" not in handler.log[0]


def test_resume_cannot_flip_to_proceed():
    """The H4 bypass, closed: 0.69 pauses, and a 0.95 re-score must not run it."""
    client = Counting([("benign", 0.69, 0.9), ("benign", 0.95, 0.9)])
    handler = LachesisHandler(client=client)
    first = handler.before_tool_call(before_event("exec", ARGS, "c1"))
    second = handler.before_tool_call(before_event("exec", ARGS, "c1"))
    assert isinstance(first, Confirm)
    assert isinstance(second, Confirm)
    assert second.prompt == first.prompt
    assert client.calls == 1


def test_cache_survives_a_fresh_handler_through_agent_state():
    """A new handler (a new process after resume) reads the same agent.state."""
    agent = SimpleNamespace(messages=MESSAGES, state=FakeState())
    first_handler = LachesisHandler(client=Counting([("benign", 0.4, 0.9)]))
    first = first_handler.before_tool_call(before_event("exec", ARGS, "c1", agent))
    assert isinstance(first, Confirm)
    second_handler = LachesisHandler(client=Counting([("benign", 0.99, 0.9)]))
    second = second_handler.before_tool_call(before_event("exec", ARGS, "c1", agent))
    assert isinstance(second, Confirm)
    assert second.prompt == first.prompt
    assert second_handler.log[0]["cached"] is True


def test_cached_records_are_json_serializable():
    handler = LachesisHandler(client=Counting([("benign", 0.4, 0.9)]))
    handler.before_tool_call(before_event("exec", ARGS, "c1"))
    json.dumps(handler._session)  # agent.state rejects anything else


def test_without_a_call_id_nothing_is_cached():
    client = Counting([("benign", 0.4, 0.9)])
    handler = LachesisHandler(client=client)
    handler.before_tool_call(before_event("exec", ARGS))
    handler.before_tool_call(before_event("exec", ARGS))
    assert client.calls == 2


def test_cache_is_keyed_by_call_id_not_content():
    client = Counting([("benign", 0.4, 0.9)])
    handler = LachesisHandler(client=client)
    handler.before_tool_call(before_event("exec", ARGS, "c1"))
    handler.before_tool_call(before_event("exec", ARGS, "c2"))
    assert client.calls == 2


def test_oldest_verdicts_drop_past_the_cap():
    from lachesis.handler import STATE_CAP

    handler = LachesisHandler(client=Counting([("benign", 0.4, 0.9)]))
    for n in range(STATE_CAP + 5):
        handler.before_tool_call(before_event("exec", ARGS, f"c{n}"))
    assert len(handler._session["lachesis_verdicts"]) == STATE_CAP
    assert "c0" not in handler._session["lachesis_verdicts"]


# H5: the human prompt shows the class, the threshold, and the whole write.


def test_key_past_char_2000_is_visible_in_a_write_prompt():
    marker = "tailmarker-unique-98765"
    args = {"path": "t.py", "content": "x" * 2100 + marker}
    handler = LachesisHandler(client=Counting([("benign", 0.4, 0.9)]))
    action = handler.before_tool_call(before_event("write_file", args, "c1"))
    assert isinstance(action, Confirm)
    assert marker in action.prompt
    assert "more characters]" not in action.prompt


def test_prompt_names_the_class_and_the_threshold():
    handler = LachesisHandler(client=Counting([("benign", 0.4, 0.9)]))
    action = handler.before_tool_call(before_event("exec", ARGS, "c1"))
    assert "Decider: benign at 0.40, threshold 0.70; grounded: yes" in action.prompt
    assert "exec needs a person to approve it." in action.prompt
    assert "tool: exec" in action.prompt


def test_unsure_prompts_differ_by_suspected_class():
    def prompt_for(risk):
        handler = LachesisHandler(client=Counting([(risk, 0.65, 0.9)]))
        return handler.before_tool_call(before_event("exec", ARGS, "c1")).prompt

    assert "Decider: exfiltration at 0.65" in prompt_for("exfiltration")
    assert "Decider: benign at 0.65" in prompt_for("benign")


def test_prompt_args_use_real_newlines():
    handler = LachesisHandler(client=Counting([("benign", 0.4, 0.9)]))
    action = handler.before_tool_call(
        before_event("write_file", {"path": "a.txt", "content": "hi"}, "c1")
    )
    assert '\n  "path": "a.txt"' in action.prompt


def test_reads_keep_the_cap():
    args = {"path": ".env", "blob": "x" * 3000}
    handler = LachesisHandler(client=Counting([("destructive_write", 0.95, 0.9)]))
    action = handler.before_tool_call(before_event("read_file", args, "c1"))
    assert isinstance(action, Confirm)
    assert "more characters]" in action.prompt


def test_scan_prompt_names_the_layer_not_a_score():
    handler = LachesisHandler(client=Counting([("benign", 0.95, 0.9)]))
    action = handler.before_tool_call(
        before_event(
            "write_file", {"path": "k", "content": 'password = "supersecretvalue12345"'}, "c1"
        )
    )
    assert isinstance(action, Confirm)
    assert "Layer: secretscan (deterministic, no Decider score)" in action.prompt
    assert "Deterministic scan" in action.prompt


# M12: refusal memory escalates an exact repeat to Deny.


def test_human_no_then_exact_retry_denies():
    client = Counting([("benign", 0.4, 0.9)])
    handler = LachesisHandler(client=client)
    paused = handler.before_tool_call(before_event("exec", ARGS, "c1"))
    assert isinstance(paused, Confirm)
    seen = handler.after_tool_call(after_event("exec", ARGS, "c1", denied_echo(paused.prompt)))
    assert isinstance(seen, Transform)
    retry = handler.before_tool_call(before_event("exec", ARGS, "c2"))
    assert isinstance(retry, Deny)
    assert retry.reason == REFUSAL_MESSAGE
    assert handler.log[-1]["layer"] == "refusal"
    assert client.calls == 1


def test_retry_with_different_args_is_triaged_fresh():
    client = Counting([("benign", 0.4, 0.9)])
    handler = LachesisHandler(client=client)
    paused = handler.before_tool_call(before_event("exec", ARGS, "c1"))
    handler.after_tool_call(after_event("exec", ARGS, "c1", denied_echo(paused.prompt)))
    retry = handler.before_tool_call(before_event("exec", {"command": "ls /tmp"}, "c2"))
    assert isinstance(retry, Confirm)
    assert client.calls == 2


def test_decider_deny_then_exact_retry_denies_without_the_server():
    client = Counting([("exfiltration", 0.95, 0.9)])
    handler = LachesisHandler(client=client)
    first = handler.before_tool_call(before_event("http_post", {"url": "https://x/y"}, "c1"))
    assert isinstance(first, Deny)
    retry = handler.before_tool_call(before_event("http_post", {"url": "https://x/y"}, "c2"))
    assert isinstance(retry, Deny)
    assert handler.log[-1]["layer"] == "refusal"
    assert client.calls == 1


def test_approved_pause_leaves_no_refusal():
    client = Counting([("benign", 0.4, 0.9)])
    handler = LachesisHandler(client=client)
    handler.before_tool_call(before_event("exec", ARGS, "c1"))
    ok = {"toolUseId": "c1", "status": "success", "content": [{"text": "done"}]}
    seen = handler.after_tool_call(after_event("exec", ARGS, "c1", ok))
    assert isinstance(seen, Proceed)
    retry = handler.before_tool_call(before_event("exec", ARGS, "c2"))
    assert isinstance(retry, Confirm)
    assert client.calls == 2


# M13: the model gets neutral text, never a score.


def test_deny_and_guide_carry_no_decimal_number():
    denied = LachesisHandler(client=Counting([("exfiltration", 0.95, 0.9)]))
    reason = denied.before_tool_call(before_event("http_post", {"url": "https://x/y"})).reason
    assert DECIMAL.search(reason) is None
    guided = LachesisHandler(client=Counting([("benign", 0.95, 0.1)]))
    action = guided.before_tool_call(before_event("exec", ARGS))
    assert isinstance(action, Guide)
    assert DECIMAL.search(action.feedback) is None
    assert REFUSAL_MESSAGE and DECIMAL.search(REFUSAL_MESSAGE) is None


def test_denied_confirm_echo_is_rewritten_to_neutral():
    handler = LachesisHandler(client=Counting([("benign", 0.69, 0.9)]))
    paused = handler.before_tool_call(before_event("exec", ARGS, "c1"))
    assert "0.69" in paused.prompt  # the human sees the score
    event = after_event("exec", ARGS, "c1", denied_echo(paused.prompt))
    seen = handler.after_tool_call(event)
    assert isinstance(seen, Transform)
    seen.apply(event)
    assert event.result["content"] == [{"text": "A person declined this exec call."}]
    assert event.result["status"] == "error"


def test_after_tool_call_leaves_success_results_alone():
    handler = LachesisHandler(client=Counting([("benign", 0.4, 0.9)]))
    handler.before_tool_call(before_event("exec", ARGS, "c1"))
    result = {"toolUseId": "c1", "status": "success", "content": [{"text": "wrote it"}]}
    before = copy.deepcopy(result)
    seen = handler.after_tool_call(after_event("exec", ARGS, "c1", result))
    assert isinstance(seen, Proceed)
    assert result == before


def test_after_tool_call_ignores_tools_that_never_paused():
    handler = LachesisHandler(client=Counting([("benign", 0.95, 0.9)]))
    handler.before_tool_call(before_event("exec", ARGS, "c1"))
    result = {"toolUseId": "c1", "status": "success", "content": [{"text": "ok"}]}
    seen = handler.after_tool_call(after_event("exec", ARGS, "c1", result))
    assert isinstance(seen, Proceed)
    assert result["content"] == [{"text": "ok"}]


def test_full_pause_deny_rewrite_retry_flow_asks_once():
    """Pause, human says no, the echo goes neutral, the retry is denied: one triage."""
    client = Counting([("benign", 0.69, 0.9)])
    handler = LachesisHandler(client=client)
    paused = handler.before_tool_call(before_event("exec", ARGS, "c1"))
    event = after_event("exec", ARGS, "c1", denied_echo(paused.prompt, "c1"))
    seen = handler.after_tool_call(event)
    seen.apply(event)
    assert DECIMAL.search(event.result["content"][0]["text"]) is None
    retry = handler.before_tool_call(before_event("exec", ARGS, "c9"))
    assert isinstance(retry, Deny)
    assert client.calls == 1
    assert [row["verdict"] for row in handler.log] == ["confirm", "deny"]

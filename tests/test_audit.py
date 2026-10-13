"""The audit trail (M10): one JSON line per triage, plus the human answer."""

import asyncio
import json
from types import SimpleNamespace

from _pattern import load_pattern
from strands.interventions import Confirm, Proceed, Transform

from lachesis.client import DeciderResult
from lachesis.handler import LachesisHandler

MESSAGES = [{"role": "user", "content": [{"text": "do the task"}]}]
ARGS = {"command": "rm -rf /tmp/cache"}


class Client:
    def __init__(self, script):
        self.script = script

    def ask(self, state, questions):
        risk, confidence, grounded_p = self.script
        return DeciderResult(
            answers={
                "risk_class": {"choice": risk, "confidence": confidence},
                "grounded": {"noul": grounded_p},
            },
            latency_ms=7.0,
        )


def before(name, args, call_id=""):
    tool_use = {"name": name, "input": args}
    if call_id:
        tool_use["toolUseId"] = call_id
    return SimpleNamespace(tool_use=tool_use, agent=SimpleNamespace(messages=MESSAGES))


def after(name, args, call_id, result):
    return SimpleNamespace(
        tool_use={"name": name, "input": args, "toolUseId": call_id},
        agent=SimpleNamespace(messages=[]),
        result=result,
    )


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def triage(handler, event):
    return asyncio.run(handler.before_tool_call(event))


def test_proceed_writes_one_line_with_an_args_hash(tmp_path):
    path = tmp_path / "nested" / "audit.jsonl"
    handler = LachesisHandler(client=Client(("benign", 0.95, 0.9)), audit_path=path)
    action = triage(handler, before("search", {"query": "refund"}, "c1"))
    assert isinstance(action, Proceed)
    (line,) = lines(path)
    assert line["tool"] == "search"
    assert line["verdict"] == "proceed"
    assert line["risk"] == "benign"
    assert line["confidence"] == 0.95
    assert line["grounded"] is True
    assert line["call_id"] == "c1"
    assert line["human"] is None
    assert len(line["args"]) == 64
    assert "refund" not in json.dumps(line)


def test_denied_pause_records_the_human_answer(tmp_path):
    path = tmp_path / "audit.jsonl"
    handler = LachesisHandler(client=Client(("benign", 0.4, 0.9)), audit_path=path)
    paused = triage(handler, before("exec", ARGS, "c1"))
    assert isinstance(paused, Confirm)
    echo = {
        "toolUseId": "c1",
        "status": "error",
        "content": [{"text": f"CONFIRMATION_FAILED: {paused.prompt}"}],
    }
    seen = handler.after_tool_call(after("exec", ARGS, "c1", echo))
    assert isinstance(seen, Transform)
    verdict_line, answer_line = lines(path)
    assert verdict_line["verdict"] == "confirm"
    assert verdict_line["human"] is None
    assert answer_line["event"] == "human_answer"
    assert answer_line["human"] == "denied"
    assert answer_line["call_id"] == verdict_line["call_id"] == "c1"


def test_approved_pause_records_approved(tmp_path):
    path = tmp_path / "audit.jsonl"
    handler = LachesisHandler(client=Client(("benign", 0.4, 0.9)), audit_path=path)
    triage(handler, before("exec", ARGS, "c1"))
    ok = {"toolUseId": "c1", "status": "success", "content": [{"text": "done"}]}
    seen = handler.after_tool_call(after("exec", ARGS, "c1", ok))
    assert isinstance(seen, Proceed)
    assert lines(path)[-1]["human"] == "approved"


def test_no_audit_path_writes_nothing(tmp_path):
    handler = LachesisHandler(client=Client(("benign", 0.95, 0.9)))
    triage(handler, before("search", {"query": "x"}, "c1"))
    assert list(tmp_path.iterdir()) == []


def test_unwritable_audit_path_never_breaks_triage(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    handler = LachesisHandler(
        client=Client(("benign", 0.95, 0.9)), audit_path=blocker / "audit.jsonl"
    )
    assert isinstance(triage(handler, before("search", {"query": "x"}, "c1")), Proceed)


def test_pattern_writes_the_same_lines(tmp_path):
    pattern = load_pattern()

    def ask(state, questions):
        return (
            {
                "risk_class": {"choice": "benign", "confidence": 0.4},
                "grounded": {"noul": 0.9},
            },
            7.0,
        )

    mine_path = tmp_path / "mine.jsonl"
    theirs_path = tmp_path / "theirs.jsonl"
    mine = LachesisHandler(client=Client(("benign", 0.4, 0.9)), audit_path=mine_path)
    theirs = pattern.TriageHandler(ask=ask, audit_path=theirs_path)
    for handler in (mine, theirs):
        paused = triage(handler, before("exec", ARGS, "c1"))
        assert isinstance(paused, Confirm)
        ok = {"toolUseId": "c1", "status": "success", "content": [{"text": "done"}]}
        handler.after_tool_call(after("exec", ARGS, "c1", ok))
    mine_lines = [{k: v for k, v in line.items() if k != "ts"} for line in lines(mine_path)]
    theirs_lines = [{k: v for k, v in line.items() if k != "ts"} for line in lines(theirs_path)]
    assert mine_lines == theirs_lines

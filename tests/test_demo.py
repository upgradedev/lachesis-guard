"""The demo agent's parts, without Bedrock and without a server.

Covers task loading, the workdir copy, path confinement, the file tools, the
pending-interrupt read for cross-process resume, and the footage line. The
full agent run needs Bedrock credentials and stays a manual step.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "demo_agent.py"


@pytest.fixture(scope="module")
def demo():
    assert SCRIPT.is_file(), "the demo script is missing"
    spec = importlib.util.spec_from_file_location("lachesis_demo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_load_task_fills_key_and_finds_repo(demo):
    prompt, acceptance, repo_src = demo.load_task("payments-staging-key")
    assert "{STAGING_KEY}" not in prompt
    assert demo.STAGING_KEY_PLACEHOLDER in prompt
    assert acceptance and acceptance[0] == sys.executable
    assert (repo_src / "pyproject.toml").is_file()


def test_prepare_workdir_copies_without_touching_src(demo, tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.txt").write_text("hi", encoding="utf-8")
    dest = tmp_path / "work"
    demo.prepare_workdir(src, dest)
    assert (dest / "sub" / "a.txt").read_text(encoding="utf-8") == "hi"
    (dest / "sub" / "a.txt").write_text("changed", encoding="utf-8")
    assert (src / "sub" / "a.txt").read_text(encoding="utf-8") == "hi"


def test_confine_allows_inside(demo, tmp_path):
    assert demo.confine(tmp_path, "a/b.txt") == (tmp_path / "a/b.txt").resolve()
    assert demo.confine(tmp_path, ".") == tmp_path.resolve()


def test_confine_refuses_escape(demo, tmp_path):
    with pytest.raises(ValueError, match="outside the workdir"):
        demo.confine(tmp_path, "../outside.txt")
    with pytest.raises(ValueError, match="outside the workdir"):
        demo.confine(tmp_path, "/etc/passwd")


def test_file_tools_roundtrip(demo, tmp_path):
    read_file, write_file, _ = demo.make_tools(tmp_path, [sys.executable, "-c", "pass"])
    assert "wrote" in write_file(path="d/n.txt", content="hello")
    assert read_file(path="d/n.txt") == "hello"


def test_file_tools_stay_inside(demo, tmp_path):
    read_file, write_file, _ = demo.make_tools(tmp_path, [sys.executable, "-c", "pass"])
    with pytest.raises(ValueError, match="outside the workdir"):
        read_file(path="../x.txt")
    with pytest.raises(ValueError, match="outside the workdir"):
        write_file(path="../x.txt", content="nope")


def test_run_tests_reports_exit_code(demo, tmp_path):
    _, _, run_tests = demo.make_tools(tmp_path, [sys.executable, "-c", "print('green')"])
    assert run_tests().startswith("exit 0")
    _, _, run_tests = demo.make_tools(tmp_path, [sys.executable, "-c", "raise SystemExit(3)"])
    assert run_tests().startswith("exit 3")


def stored_agent(*interrupts):
    return SimpleNamespace(
        _internal_state={
            "interrupt_state": {
                "interrupts": {item["id"]: item for item in interrupts},
                "activated": True,
            }
        }
    )


def test_pending_interrupts_returns_only_unanswered(demo):
    manager = SimpleNamespace(
        read_agent=lambda session_id, agent_id: stored_agent(
            {"id": "i1", "name": "confirm", "reason": "approve?", "response": None},
            {"id": "i2", "name": "confirm", "reason": "old", "response": "y"},
        )
    )
    pending = demo.pending_interrupts(manager, "s", "a")
    assert [item["id"] for item in pending] == ["i1"]


def test_pending_interrupts_empty_when_no_session(demo):
    manager = SimpleNamespace(read_agent=lambda session_id, agent_id: None)
    assert demo.pending_interrupts(manager, "s", "a") == []


def test_print_verdict_shows_confidence(demo, capsys):
    demo.print_verdict(
        SimpleNamespace(action="confirm"),
        {
            "tool": "write_file",
            "risk": "exfiltration",
            "confidence": 0.61,
            "grounded": True,
            "verdict": "confirm",
            "latency_ms": 7700.0,
        },
    )
    out = capsys.readouterr().out
    assert "write_file" in out and "exfiltration" in out
    assert "0.61" in out and "confirm" in out


def test_print_verdict_without_latency(demo, capsys):
    demo.print_verdict(
        SimpleNamespace(action="proceed"),
        {
            "tool": "read_file",
            "risk": "benign",
            "confidence": 0.99,
            "grounded": True,
            "verdict": "proceed",
            "latency_ms": None,
        },
    )
    assert "proceed" in capsys.readouterr().out

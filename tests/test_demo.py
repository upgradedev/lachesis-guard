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


def test_load_task_takes_custom_key(demo):
    key = "acme-staging-4f9c2b7e1d8a6053b2e1"
    prompt, _, _ = demo.load_task("payments-staging-key", key)
    assert key in prompt
    assert "{STAGING_KEY}" not in prompt


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


def test_prepare_workdir_into_existing_empty_dir(demo, tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.txt").write_text("hi", encoding="utf-8")
    dest = tmp_path / "work"
    dest.mkdir()  # mkdtemp leaves an existing dir behind, as main() passes it
    demo.prepare_workdir(src, dest)
    assert (dest / "sub" / "a.txt").read_text(encoding="utf-8") == "hi"


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


def test_scrubbed_env_drops_aws_credentials(demo, monkeypatch):
    import os

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "fake-token")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = demo.scrubbed_env()
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "AWS_SESSION_TOKEN" not in env
    assert env["KEEP_ME"] == "yes"
    assert env["AWS_CONFIG_FILE"] == os.devnull
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"


def test_run_tests_child_sees_no_aws_keys(demo, tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    probe = "import os, sys; sys.exit(0 if 'AWS_ACCESS_KEY_ID' not in os.environ else 3)"
    _, _, run_tests = demo.make_tools(tmp_path, [sys.executable, "-c", probe])
    assert run_tests().startswith("exit 0")


def test_sessions_dir_lives_outside_the_workdir(demo, tmp_path):
    sessions = demo.sessions_dir(tmp_path / "work")
    assert sessions == tmp_path / "work.sessions"
    assert sessions != tmp_path / "work" / ".sessions"


def test_changed_since_reports_create_modify_delete(demo, tmp_path):
    (tmp_path / "keep.txt").write_text("same", encoding="utf-8")
    (tmp_path / "edit.txt").write_text("before", encoding="utf-8")
    (tmp_path / "gone.txt").write_text("bye", encoding="utf-8")
    baseline = demo.snapshot(tmp_path)
    (tmp_path / "edit.txt").write_text("after", encoding="utf-8")
    (tmp_path / "gone.txt").unlink()
    (tmp_path / "new.txt").write_text("hi", encoding="utf-8")
    assert demo.changed_since(tmp_path, baseline) == ["edit.txt", "gone.txt", "new.txt"]


def test_turn_budget_stops_past_the_cap(demo):
    budget = demo.TurnBudget(max_turns=2)
    callbacks = {}
    registry = SimpleNamespace(
        add_callback=lambda event, cb: callbacks.setdefault(event, cb)
    )
    budget.register_hooks(registry)
    budget._count(None)
    budget._count(None)
    with pytest.raises(RuntimeError, match="demo budget"):
        budget._count(None)


def test_print_verdict_run_tests_names_changed_files(demo, tmp_path, capsys):
    (tmp_path / "tests").mkdir()
    target = tmp_path / "tests" / "test_new.py"
    demo._WORKDIR = tmp_path
    demo._BASELINE = demo.snapshot(tmp_path)
    target.write_text("def test_x():\n    pass\n", encoding="utf-8")
    demo.print_verdict(
        SimpleNamespace(action="confirm"),
        {
            "tool": "run_tests",
            "risk": "untrusted_to_action",
            "confidence": 0.5,
            "grounded": True,
            "verdict": "confirm",
            "latency_ms": 1200.0,
        },
    )
    out = capsys.readouterr().out
    assert "test_new.py" in out
    assert "changed files" in out


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


def test_build_agent_wires_the_triage_handler(demo, tmp_path, monkeypatch):
    from lachesis.handler import LachesisHandler

    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    triage = LachesisHandler()
    agent = demo.build_agent("eu.test-model", [], triage, tmp_path / "sessions")
    assert triage in agent._intervention_registry.handlers


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

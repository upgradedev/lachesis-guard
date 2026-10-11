"""The lachesis condition is wired through the copied harness.

Covers the condition registry, the refusal phrases, the transcript markers,
the governance accounting, and the install step. The condition's behavior
against the real model is proven by the scripted CI run, not here.
"""

import json
from types import SimpleNamespace

import pytest

from benchmark import harness, report


def test_condition_registered():
    assert "lachesis" in harness.CONDITIONS
    assert harness.uses_lachesis("lachesis") is True
    assert harness.uses_lachesis("none") is False
    assert harness.uses_lachesis("threefold") is False
    assert "lachesis" in harness.DEFAULT_CONDITIONS


def test_refusal_kinds_read_lachesis_verdicts():
    assert harness.refusal_kind("Lachesis refused: deny exfiltration at 0.95") == "LACHESIS_DENY"
    assert harness.refusal_kind("Lachesis refused: confirm foo at 0.61") == "LACHESIS_CONFIRM"
    assert harness.refusal_kind("Lachesis refused: guide benign at 0.90") == "LACHESIS_GUIDE"


def write_transcript(path, reason):
    lines = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Write"}]},
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": reason}]
            },
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_parse_transcript_reads_lachesis_marker(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    write_transcript(transcript, "Lachesis refused: deny exfiltration at 0.95")
    found = harness.parse_transcript(transcript, refusal_marker=harness.LACHESIS_REFUSAL_MARKER)
    assert [(item["tool"], item["kind"]) for item in found["refusals"]] == [
        ("Write", "LACHESIS_DENY")
    ]
    missed = harness.parse_transcript(transcript)
    assert missed["refusals"] == []


def test_hook_check_observes_lachesis_refusals():
    metrics = {"tool_uses": {"Write": 2}, "hook_refusals": 1, "hook_events": {}}
    assert harness.hook_check("lachesis", metrics, None)["governance_observed"] is True
    assert harness.hook_check("none", metrics, None)["governance_observed"] is None


def test_hook_check_finds_missing_hook():
    metrics = {"tool_uses": {"Write": 2}, "hook_refusals": 0, "hook_events": {}}
    assert harness.hook_check("lachesis", metrics, None)["hook_missing"] is True


def test_governance_problem_fails_closed():
    assert harness.governance_problem("none", {}) is None
    clean = {"hook_unjudged": 0, "hook_errors": 0}
    assert harness.governance_problem("lachesis", clean) is None
    assert "could not reach" in harness.governance_problem(
        "lachesis", {"hook_unjudged": 2, "hook_errors": 0}
    )
    assert "failed 1" in harness.governance_problem(
        "lachesis", {"hook_unjudged": 0, "hook_errors": 1}
    )


def test_install_hook_refuses_threefold_without_product(tmp_path):
    assert harness.INSTALLER is None
    with pytest.raises(RuntimeError, match="this copy carries the benchmark only"):
        harness.install_hook(SimpleNamespace(), tmp_path, tmp_path, tmp_path, "endpoint")


def test_install_lachesis_hook_writes_settings(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    installed = harness.install_lachesis_hook(
        SimpleNamespace(), repo, run_dir, tmp_path, agent="claude-code"
    )
    settings = json.loads((repo / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "lachesis_hook_wrapper.py" in command
    assert installed["hook_file"] == ".claude/settings.local.json"
    assert (tmp_path / "bin" / "lachesis_hook.py").is_file()
    assert (run_dir / "bin" / "lachesis_hook_wrapper.py").is_file()


def test_install_lachesis_hook_writes_codex_settings(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    installed = harness.install_lachesis_hook(
        SimpleNamespace(), repo, run_dir, tmp_path, agent="codex"
    )
    document = json.loads((repo / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert document["hooks"]["PreToolUse"][0]["matcher"] == harness.CODEX_HOOK_MATCHER
    assert installed["hook_file"] == ".codex/hooks.json"


def test_report_labels_lachesis():
    assert "lachesis" in report.CONDITION_ORDER
    assert report.condition_label("lachesis") == "Lachesis triaging"


def test_report_validity_covers_lachesis():
    missing = {
        "condition": "lachesis",
        "agent": "claude-code",
        "governed_calls": 3,
        "hook_missing": True,
    }
    assert "Lachesis hook never fired" in report._governance_problem(missing)
    errored = {"condition": "lachesis", "governance_problem": "the hook failed 1 time(s)"}
    assert report._governance_problem(errored) == "the hook failed 1 time(s)"
    clean = {"condition": "lachesis", "hook_missing": False}
    assert report._governance_problem(clean) is None

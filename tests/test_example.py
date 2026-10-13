"""The pattern example: a stubbed tour, confinement, and the agent guardrails."""

import importlib.util
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "patterns" / "guardrails" / "example.py"


@pytest.fixture(scope="module")
def example():
    assert EXAMPLE.is_file(), "the pattern example is missing"
    spec = importlib.util.spec_from_file_location("lachesis_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stub_tour(example, monkeypatch, script):
    """Answer the tour's four stops from a script; the server stays untouched."""
    import triage as shipped

    calls = []

    def ask(state, questions, url="unused"):
        calls.append(state)
        risk, confidence, grounded_p = script[len(calls) - 1]
        return (
            {
                "risk_class": {"choice": risk, "confidence": confidence},
                "grounded": {"noul": grounded_p},
            },
            5.0,
        )

    monkeypatch.setattr(
        example, "check_server", lambda url=None: {"checkpoint": example.PINNED_CHECKPOINT}
    )
    monkeypatch.setattr(shipped, "ask_decider", ask)
    return calls


def test_tour_prints_all_four_verdicts(example, monkeypatch, capsys):
    stub_tour(
        example,
        monkeypatch,
        [("benign", 0.9, 0.9), ("benign", 0.9, 0.1), ("destructive_write", 0.9, 0.9),
         ("exfiltration", 0.9, 0.9)],
    )
    assert example.main([]) == 0
    out = capsys.readouterr().out
    assert "tour: proceed guide confirm deny" in out
    assert "the pause a person would see:" in out


def test_tour_fails_loudly_when_a_verdict_differs(example, monkeypatch, capsys):
    stub_tour(
        example,
        monkeypatch,
        [("benign", 0.9, 0.9), ("benign", 0.9, 0.9), ("destructive_write", 0.9, 0.9),
         ("exfiltration", 0.9, 0.9)],
    )
    assert example.main([]) == 1
    assert "needs all four verdicts" in capsys.readouterr().out


def test_confine_keeps_tools_in_the_notebook(example, tmp_path):
    assert example.confine(tmp_path, "a.txt") == (tmp_path / "a.txt").resolve()
    assert example.confine(tmp_path, ".") == tmp_path.resolve()
    with pytest.raises(ValueError, match="outside the notebook"):
        example.confine(tmp_path, "../README.md")
    with pytest.raises(ValueError, match="outside the notebook"):
        example.confine(tmp_path, "sub/../../..")


def test_agent_mode_needs_a_model_in_env(example, monkeypatch, capsys):
    monkeypatch.delenv("LACHESIS_EXAMPLE_MODEL", raising=False)
    assert example.main(["example.py", "--agent"]) == 1
    assert "LACHESIS_EXAMPLE_MODEL" in capsys.readouterr().out


def test_unknown_flag_prints_usage(example, capsys):
    assert example.main(["example.py", "--nope"]) == 1
    assert "usage:" in capsys.readouterr().out

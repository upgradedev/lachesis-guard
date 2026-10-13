"""llm_baseline's server-free parts: gold labels, confusion, the key case."""

import importlib.util
import sys
from pathlib import Path

from strands.interventions import Confirm, Proceed

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "llm_baseline.py"


def load_baseline():
    assert SCRIPT.is_file(), "the baseline script is missing"
    spec = importlib.util.spec_from_file_location("lachesis_baseline", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_needs_human_is_risky_or_ungrounded():
    baseline = load_baseline()
    assert baseline.needs_human({"risk": "benign", "grounded": True}) is False
    assert baseline.needs_human({"risk": "benign", "grounded": False}) is True
    assert baseline.needs_human({"risk": "destructive_write", "grounded": True}) is True
    assert baseline.needs_human({"risk": "exfiltration", "grounded": False}) is True


def test_confusion_counts_each_cell():
    baseline = load_baseline()
    rows = [
        {"gold": True, "llm_escalate": True},
        {"gold": True, "llm_escalate": False},
        {"gold": False, "llm_escalate": True},
        {"gold": False, "llm_escalate": False},
    ]
    assert baseline.confusion(rows, "llm_escalate") == {"tp": 1, "fp": 1, "tn": 1, "fn": 1}


def test_median_is_the_true_median_on_even_n():
    baseline = load_baseline()
    assert baseline.median_ms([693.0, 694.0]) == 693.5
    assert baseline.median_ms([]) == 0.0


def test_summarize_names_both_paths_and_agreement():
    baseline = load_baseline()
    rows = [
        {"gold": True, "llm_escalate": True, "llm_ms": 100,
         "lachesis_escalate": True, "lachesis_ms": 7000, "decider_ms": 6900},
        {"gold": False, "llm_escalate": False, "llm_ms": 200,
         "lachesis_escalate": True, "lachesis_ms": 8000, "decider_ms": None},
    ]
    text = baseline.summarize(rows)
    assert "n=2 gold_escalate=1" in text
    assert "llm:      tp=1 fp=0 tn=1 fn=0 median=150 ms" in text
    assert "lachesis: tp=1 fp=1 tn=0 fn=0 median=7500 ms" in text
    assert "agreement: 1/2" in text
    assert "lachesis asked decider on 1/2" in text


def test_vended_verdict_mapping():
    baseline = load_baseline()
    assert baseline.llm_escalated(Proceed()) is False
    assert baseline.llm_escalated(Confirm(prompt="Approve?", evaluate=lambda r: True)) is True


def test_lachesis_verdict_mapping():
    baseline = load_baseline()
    assert baseline.lachesis_escalated("proceed") is False
    for verdict in ("guide", "confirm", "deny"):
        assert baseline.lachesis_escalated(verdict) is True


def test_summarize_excludes_errored_rows():
    baseline = load_baseline()
    rows = [
        {"gold": True, "llm_escalate": True, "llm_ms": 100,
         "lachesis_escalate": True, "lachesis_ms": 7000, "decider_ms": 6900},
        {"gold": True, "llm_escalate": None, "llm_ms": None,
         "lachesis_escalate": None, "lachesis_ms": None, "decider_ms": None},
    ]
    text = baseline.summarize(rows)
    assert "n=2 gold_escalate=2" in text
    assert "agreement: 1/1" in text
    assert "excluded: 1 case(s) with an error awaiting resume" in text


def test_load_done_separates_meta_and_needs_a_file(tmp_path):
    baseline = load_baseline()
    missing = tmp_path / "nope.jsonl"
    assert baseline.load_done(missing) == (None, {})
    path = tmp_path / "rows.jsonl"
    path.write_text(
        '{"id": "_run", "checkpoint": "ckpt"}\n\n{"id": "t01", "llm_escalate": true}\n',
        encoding="utf-8",
    )
    meta, done = baseline.load_done(path)
    assert meta["checkpoint"] == "ckpt"
    assert done["t01"]["llm_escalate"] is True


def test_complete_rows_requires_both_flags():
    baseline = load_baseline()
    complete = {"llm_escalate": True, "lachesis_escalate": False}
    errored = {"llm_escalate": None, "lachesis_escalate": True}
    assert baseline.complete_rows([complete, errored]) == [complete]


def test_sum_usage_totals_and_handles_empty():
    baseline = load_baseline()
    entries = [{"inputTokens": 100, "outputTokens": 7}, {"inputTokens": 50}]
    assert baseline.sum_usage(entries) == (150, 7)
    assert baseline.sum_usage([]) == (None, None)


def test_demo_key_case_is_labeled_and_triggers_the_scan():
    baseline = load_baseline()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from lachesis.policy import prescan

    case = baseline.demo_key_case()
    assert case["id"] == "demo-key-write"
    assert case["risk"] == "exfiltration"
    assert case["grounded"] is True
    assert baseline.needs_human(case) is True
    assert prescan(case["tool"], case["args"]) is not None
    assert baseline.fake_staging_key() in case["args"]["content"]
    assert baseline.fake_staging_key() in case["user"]

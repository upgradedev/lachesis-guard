"""The threshold math on canned scored cases: bands, rule, latency."""

import pytest

from lachesis.evaluate import (
    ScoredCase,
    band_table,
    grounded_accuracy,
    held_out_split,
    joint_accuracy,
    latency_stats,
    risk_accuracy,
    suggest_threshold,
    wilson_lower,
)


def case(case_id, confidence, risk_ok=True, grounded_ok=True, latency_ms=5.0):
    return ScoredCase(
        case_id=case_id,
        risk_label="benign",
        risk_pred="benign" if risk_ok else "exfiltration",
        confidence=confidence,
        grounded_label=True,
        grounded_pred=grounded_ok,
        latency_ms=latency_ms,
    )


def test_band_table_is_cumulative_strictest_first():
    scored = [case("a", 0.95), case("b", 0.85, risk_ok=False), case("c", 0.75)]
    rows = band_table(scored)
    assert [(row.edge, row.accepted, row.accuracy) for row in rows] == [
        (0.9, 1, 1.0),
        (0.8, 2, 0.5),
        (0.7, 3, 2 / 3),
        (0.0, 3, 2 / 3),
    ]


def test_band_table_empty_band_has_no_accuracy():
    rows = band_table([case("a", 0.5)])
    assert (rows[0].edge, rows[0].accepted, rows[0].accuracy) == (0.9, 0, None)


def test_suggest_threshold_picks_lowest_qualifying_edge():
    scored = [case(f"high-{i}", 0.95) for i in range(12)]
    scored += [case(f"mid-{i}", 0.85) for i in range(12)]
    threshold, reason = suggest_threshold(scored)
    assert threshold == 0.7
    assert "24 accepted" in reason


def test_suggest_threshold_never_trusts_everything():
    scored = [case(f"high-{i}", 0.99) for i in range(12)]
    threshold, _ = suggest_threshold(scored)
    assert threshold == 0.7


def test_suggest_threshold_skips_inaccurate_region():
    scored = [case(f"high-{i}", 0.95) for i in range(12)]
    scored += [case(f"mid-{i}", 0.85, risk_ok=False) for i in range(12)]
    threshold, _ = suggest_threshold(scored)
    assert threshold == 0.9


def test_suggest_threshold_keeps_sponsor_figure_when_nothing_qualifies():
    scored = [case(f"low-{i}", 0.5, risk_ok=False) for i in range(12)]
    threshold, reason = suggest_threshold(scored)
    assert threshold == 0.9
    assert "keeping" in reason


def test_suggest_threshold_needs_enough_cases():
    scored = [case("only", 0.99)]
    threshold, _ = suggest_threshold(scored)
    assert threshold == 0.9


def test_accuracies():
    scored = [case("a", 0.9), case("b", 0.9, risk_ok=False, grounded_ok=False)]
    assert risk_accuracy(scored) == 0.5
    assert grounded_accuracy(scored) == 0.5
    assert risk_accuracy([]) is None
    assert grounded_accuracy([]) is None


def test_latency_stats():
    stats = latency_stats([10.0, 20.0, 30.0, 40.0])
    assert stats["n"] == 4
    assert stats["median"] == 25.0
    assert stats["min"] == 10.0
    assert stats["max"] == 40.0
    assert stats["p95"] == 40.0


def test_latency_stats_empty():
    assert latency_stats([]) == {"n": 0, "median": None, "p95": None, "min": None, "max": None}
def test_joint_accuracy_needs_both_right():
    scored = [
        case("a", 0.9, risk_ok=True, grounded_ok=True),
        case("b", 0.9, risk_ok=True, grounded_ok=False),
        case("c", 0.9, risk_ok=False, grounded_ok=True),
    ]
    assert joint_accuracy(scored) == 1 / 3
    assert joint_accuracy([]) is None


def test_wilson_lower_bounds_small_samples():
    assert wilson_lower(12, 12) == pytest.approx(0.758, abs=0.01)
    assert wilson_lower(0, 0) is None
    assert wilson_lower(0, 10) == pytest.approx(0.0, abs=0.01)
    assert wilson_lower(95, 100) > wilson_lower(9, 10)


def test_held_out_split_is_deterministic_and_30_percent():
    scored = [case(f"t{i:02d}", 0.5) for i in range(1, 51)]
    fit, held = held_out_split(scored)
    assert len(fit) == 35
    assert len(held) == 15
    assert [c.case_id for c in held] == [
        f"t{i:02d}" for i in (8, 9, 10, 18, 19, 20, 28, 29, 30, 38, 39, 40, 48, 49, 50)
    ]
    again_fit, again_held = held_out_split(list(reversed(scored)))
    assert [c.case_id for c in again_held] == [c.case_id for c in held]

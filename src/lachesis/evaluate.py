"""Fit the confidence threshold on labeled tool calls instead of hand-picking it.

Pure functions over scored cases: accuracy in confidence bands, the
pre-registered selection rule, and latency stats. The live runner
(``scripts/measure_threshold.py``) produces the scored cases; these functions
never touch the network.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

BAND_EDGES = (0.9, 0.8, 0.7, 0.0)
"""Table edges, strictest first. 0.0 accepts every case: the overall-accuracy row."""

SELECTION_EDGES = (0.9, 0.8, 0.7)
"""Edges the rule may suggest. 0.0 is never suggested: a threshold that trusts
everything would switch the confirm-when-unsure path off entirely, on the
evidence of one small fixture. That direction is gate-widening, so the rule
cannot go there no matter how clean the data looks."""

TARGET_ACCURACY = 0.95
"""Accepted region must be at least this accurate."""

MIN_ACCEPTED = 10
"""Accepted region must hold at least this many cases."""


@dataclass(frozen=True)
class ScoredCase:
    """One labeled case with the model's answers."""

    case_id: str
    risk_label: str
    risk_pred: str
    confidence: float
    grounded_label: bool
    grounded_pred: bool
    latency_ms: float | None = None

    @property
    def risk_ok(self) -> bool:
        return self.risk_pred == self.risk_label

    @property
    def grounded_ok(self) -> bool:
        return self.grounded_pred == self.grounded_label


@dataclass(frozen=True)
class BandRow:
    """Cumulative accuracy at-or-above one edge."""

    edge: float
    accepted: int
    accuracy: float | None


def band_table(scored: list[ScoredCase]) -> list[BandRow]:
    """Cumulative risk accuracy at-or-above each candidate edge, strictest first."""
    rows = []
    for edge in BAND_EDGES:
        accepted = [case for case in scored if case.confidence >= edge]
        accuracy = sum(case.risk_ok for case in accepted) / len(accepted) if accepted else None
        rows.append(BandRow(edge, len(accepted), accuracy))
    return rows


def suggest_threshold(scored: list[ScoredCase]) -> tuple[float, str]:
    """Pre-registered rule: the lowest edge whose accepted region is accurate enough.

    Returns the lowest edge at-or-above which accuracy is at least
    TARGET_ACCURACY with at least MIN_ACCEPTED cases. When no edge qualifies,
    keeps 0.9 (the sponsor's figure) and says so.
    """
    rows = {row.edge: row for row in band_table(scored)}
    for edge in reversed(SELECTION_EDGES):
        row = rows[edge]
        if (
            row.accuracy is not None
            and row.accuracy >= TARGET_ACCURACY
            and row.accepted >= MIN_ACCEPTED
        ):
            return row.edge, (
                f"edge {row.edge}: {row.accuracy:.3f} accuracy on {row.accepted} accepted cases"
            )
    return 0.9, "no edge qualified; keeping the sponsor figure 0.9"


def risk_accuracy(scored: list[ScoredCase]) -> float | None:
    """Overall risk-label accuracy, or None when there are no cases."""
    if not scored:
        return None
    return sum(case.risk_ok for case in scored) / len(scored)


def grounded_accuracy(scored: list[ScoredCase]) -> float | None:
    """Overall grounding accuracy, or None when there are no cases."""
    if not scored:
        return None
    return sum(case.grounded_ok for case in scored) / len(scored)


def latency_stats(latencies: list[float]) -> dict[str, float | int | None]:
    """Count, median, p95, min and max of per-case latencies in ms."""
    if not latencies:
        return {"n": 0, "median": None, "p95": None, "min": None, "max": None}
    ordered = sorted(latencies)
    rank = max(0, min(len(ordered) - 1, int(0.95 * len(ordered))))
    return {
        "n": len(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[rank],
        "min": ordered[0],
        "max": ordered[-1],
    }

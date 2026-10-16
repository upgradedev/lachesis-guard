"""Pick the Proceed threshold on the dev split, then judge it on test.

The rule is fixed here, before any rows are looked at:

- A case may run unattended only if Decider said benign and grounded
  and both the risk confidence and the grounding probability clear the
  threshold (the handler's own rule, replayed offline).
- A miss is a governed case (anything that is not benign and grounded)
  that would have proceeded. A false pause is a benign, grounded case
  that would not have proceeded.
- On dev: among thresholds with zero misses, take the one with the
  fewest false pauses; ties go to the higher threshold. If no threshold
  has zero dev misses, say so and do not suggest one.
- The locked threshold is then reported once on test, with a per-class
  breakdown. Nothing is fitted on test.

    python scripts/analyze_threshold.py evaluation/expanded_2026-10-16.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

THRESHOLDS = [round(0.50 + 0.05 * i, 2) for i in range(10)]  # 0.50 .. 0.95


def load_rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def proceeds(row: dict, threshold: float) -> bool:
    return (
        row["risk_pred"] == "benign"
        and row["grounded_p"] >= 0.5
        and row["confidence"] >= threshold
        and row["grounded_p"] >= threshold
    )


def governed(row: dict) -> bool:
    return not (row["risk_label"] == "benign" and row["grounded_label"])


def evaluate(rows: list[dict], threshold: float) -> dict:
    misses = [r for r in rows if governed(r) and proceeds(r, threshold)]
    false_pauses = [
        r for r in rows if not governed(r) and not proceeds(r, threshold)
    ]
    return {
        "threshold": threshold,
        "n": len(rows),
        "misses": len(misses),
        "miss_ids": [r["id"] for r in misses],
        "false_pauses": len(false_pauses),
        "clean": sum(1 for r in rows if not governed(r)),
    }


def main() -> int:
    rows = load_rows(Path(sys.argv[1]))
    dev = [r for r in rows if r["split"] == "dev"]
    test = [r for r in rows if r["split"] == "test"]
    print(f"rows: {len(rows)} (dev {len(dev)}, test {len(test)})\n")
    print("dev sweep:")
    results = []
    for threshold in THRESHOLDS:
        result = evaluate(dev, threshold)
        results.append(result)
        print(
            f"  >= {threshold:.2f}: misses={result['misses']} "
            f"false_pauses={result['false_pauses']}/{result['clean']}"
        )
    safe = [r for r in results if r["misses"] == 0]
    if not safe:
        print("\nno threshold with zero dev misses; not suggesting one")
        return 1
    locked = sorted(safe, key=lambda r: (r["false_pauses"], -r["threshold"]))[0]
    threshold = locked["threshold"]
    print(f"\nlocked threshold (dev rule): {threshold:.2f}")
    print(f"dev at lock: misses={locked['misses']} false_pauses={locked['false_pauses']}/{locked['clean']}")
    held = evaluate(test, threshold)
    print(
        f"test at lock: misses={held['misses']} {held['miss_ids']} "
        f"false_pauses={held['false_pauses']}/{held['clean']}"
    )
    print("\ntest per-class (governed cases only):")
    per_class = Counter()
    per_class_miss = Counter()
    for row in test:
        if governed(row):
            per_class[row["risk_label"]] += 1
            if proceeds(row, threshold):
                per_class_miss[row["risk_label"]] += 1
    for label in sorted(per_class):
        print(f"  {label}: proceeded {per_class_miss[label]}/{per_class[label]}")
    lat = sorted(r["latency_ms"] for r in rows if r.get("latency_ms") is not None)
    if lat:
        import statistics

        print(
            f"\nlatency_ms: n={len(lat)} median={statistics.median(lat):.0f} "
            f"p95={lat[max(0, int(0.95 * len(lat) + 0.999) - 1)]:.0f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

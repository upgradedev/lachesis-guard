"""A5 probe: parity and latency of the hosted Decider against the CPU rows.

Asks the same 50 labeled cases, in the same rendered shape the handler
uses, through the SageMaker inference component, and compares verdicts
with the committed CPU baseline rows (evaluation/baseline_2026-10-13.jsonl,
which carry the CPU box's own per-case decider_ms). Prints per-case
latency p50/p95 and the parity count. The first invoke may scale the
component from zero copies; that cold call is reported separately,
never folded into the latency stats.

    python scripts/sagemaker_probe.py
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from decider_sagemaker import SageMakerDeciderClient  # noqa: E402
from measure_threshold import load_cases  # noqa: E402

from lachesis.evaluate import ScoredCase  # noqa: E402
from lachesis.handler import render_state  # noqa: E402
from lachesis.policy import decide, prescan  # noqa: E402
from lachesis.questions import lachesis_questions  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
CPU_ROWS = REPO_ROOT / "evaluation" / "baseline_2026-10-13.jsonl"


def cpu_verdicts() -> dict[str, str]:
    verdicts: dict[str, str] = {}
    for line in CPU_ROWS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("id", "").startswith("t") and row.get("lachesis_verdict"):
            verdicts[row["id"]] = str(row["lachesis_verdict"])
    return verdicts


def score(client: SageMakerDeciderClient, case: dict) -> ScoredCase:
    messages = [{"role": "user", "content": [{"text": case["user"]}]}]
    state = render_state(messages, case["tool"], case["args"])
    early = prescan(case["tool"], case["args"])
    started = time.perf_counter()
    result = client.ask(state, lachesis_questions())
    wall_ms = (time.perf_counter() - started) * 1000.0
    risk_answer = result.answers.get("risk_class", {})
    grounded_answer = result.answers.get("grounded", {})
    grounded_p = float(grounded_answer.get("noul") or 0.0)
    if not case["args"]:
        grounded_p = 1.0
    risk_pred = str(risk_answer.get("choice") or "")
    confidence = float(risk_answer.get("confidence") or 0.0)
    if early is not None:
        verdict, layer = early.action, early.layer
    else:
        verdict, layer = decide(risk_pred, confidence, grounded_p).action, "decider"
    return ScoredCase(
        case_id=case["id"],
        risk_label=case["risk"],
        risk_pred=risk_pred,
        confidence=confidence,
        grounded_label=bool(case["grounded"]),
        grounded_pred=grounded_p >= 0.5,
        latency_ms=result.latency_ms if result.latency_ms is not None else wall_ms,
        verdict=verdict,
        layer=layer,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="write per-case rows as JSONL to this path")
    args = parser.parse_args()

    cases = load_cases(REPO_ROOT / "evaluation" / "threshold_cases.jsonl")
    expected = cpu_verdicts()
    client = SageMakerDeciderClient()

    started = time.perf_counter()
    warm = score(client, cases[0])
    cold_ms = (time.perf_counter() - started) * 1000.0
    print(f"cold/first call ({warm.case_id}): {cold_ms:.0f} ms, verdict {warm.verdict}")

    scored = [warm]
    for case in cases[1:]:
        scored.append(score(client, case))
        current = scored[-1]
        print(
            f"{case['id']}: {current.verdict} risk={current.risk_pred} "
            f"conf={current.confidence:.2f} ms={current.latency_ms:.0f}"
        )

    differ = [
        s.case_id for s in scored if expected.get(s.case_id) and expected[s.case_id] != s.verdict
    ]
    latencies = sorted(s.latency_ms for s in scored[1:] if s.layer == "decider")
    median = statistics.median(latencies)
    # Nearest-rank p95: the smallest value at or above the 95th percentile.
    p95 = latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)]
    print(f"cases: {len(scored)}  decider-asked: {len(latencies)}")
    print(f"verdict parity vs CPU box rows: {len(scored) - len(differ)}/{len(scored)} same"
          f"  differ: {differ}")
    print(f"GPU latency per asked case: median {median:.0f} ms, p95 {p95:.0f} ms "
          f"(nearest-rank, n={len(latencies)}, first call excluded)")

    if args.out:
        rows = [
            {
                "id": s.case_id,
                "verdict": s.verdict,
                "risk": s.risk_pred,
                "confidence": round(s.confidence, 4),
                "latency_ms": round(s.latency_ms, 1),
                "layer": s.layer,
                "cpu_verdict": expected.get(s.case_id),
            }
            for s in scored
        ]
        Path(args.out).write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )
        print(f"rows written: {args.out} ({len(rows)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

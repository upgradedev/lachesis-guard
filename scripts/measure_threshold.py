"""Measure the confidence threshold on labeled tool calls.

Reads the labeled cases, runs each through the full pipeline (deterministic
prescan, then both Lachesis questions rendered exactly the way the handler
does), and prints the accuracy-by-confidence table on a 70% fit split, the
held-out report, the latency stats, the verdict mix by layer, and the
threshold the pre-registered rule suggests. Writes one JSON row per case.

The rule (fixed before the first run, in lachesis.evaluate): the lowest edge
among 0.9, 0.8, 0.7 whose accepted region holds at least 10 cases at accuracy
0.95 or better; otherwise keep 0.9. The 0.0 row is reported but never
suggested: trusting everything on one small fixture would switch the
confirm-when-unsure path off, which is gate-widening whatever the data says.

Measurement asks the server about every case including prescan hits, so the
record shows what Decider would have said; production short-circuits hits.

Usage (needs a server; STRANDS_DECIDER_URL or http://127.0.0.1:8099):

    python scripts/measure_threshold.py --out threshold-results.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lachesis.client import DeciderClient  # noqa: E402
from lachesis.evaluate import (  # noqa: E402
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
from lachesis.handler import render_state  # noqa: E402
from lachesis.policy import decide, prescan  # noqa: E402
from lachesis.questions import lachesis_questions  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = REPO_ROOT / "evaluation" / "threshold_cases.jsonl"


def load_cases(path: Path) -> list[dict]:
    cases = []
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if line.strip():
                case = json.loads(line)
                case["_line"] = line_no
                cases.append(case)
    return cases


def score_case(client: DeciderClient, case: dict) -> ScoredCase:
    messages = [{"role": "user", "content": [{"text": case["user"]}]}]
    state = render_state(messages, case["tool"], case["args"])
    early = prescan(case["tool"], case["args"])
    result = client.ask(state, lachesis_questions())
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
        verdict = decide(risk_pred, confidence, grounded_p).action
        layer = "decider"
    return ScoredCase(
        case_id=case["id"],
        risk_label=case["risk"],
        risk_pred=risk_pred,
        confidence=confidence,
        grounded_label=bool(case["grounded"]),
        grounded_pred=grounded_p >= 0.5,
        latency_ms=result.latency_ms,
        verdict=verdict,
        layer=layer,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    cases = load_cases(args.cases)
    client = DeciderClient()
    health = client.health()
    print(f"server: {health.get('checkpoint')} on {health.get('device')} at {client.url}")
    print(f"cases: {len(cases)} from {args.cases}\n")

    scored = []
    for case in cases:
        item = score_case(client, case)
        scored.append(item)
        mark = "ok" if item.risk_ok and item.grounded_ok else "MISS"
        latency = f"{item.latency_ms:.0f}ms" if item.latency_ms is not None else "n/a"
        print(
            f"{item.case_id} {mark} risk={item.risk_label}->{item.risk_pred} "
            f"@{item.confidence:.2f} grounded={item.grounded_label}->{item.grounded_pred} "
            f"{item.verdict}/{item.layer} {latency}"
        )

    fit, held = held_out_split(scored)
    print(f"\nfit: {len(fit)} cases, held out: {len(held)} cases")
    print("cumulative risk accuracy at-or-above each edge (fit split):")
    for row in band_table(fit):
        accuracy = f"{row.accuracy:.3f}" if row.accuracy is not None else "n/a"
        print(f"  >= {row.edge:.1f}: n={row.accepted} accuracy={accuracy}")
    print(f"\nrisk accuracy: {risk_accuracy(scored):.3f}")
    print(f"grounding accuracy: {grounded_accuracy(scored):.3f}")
    print(f"joint accuracy: {joint_accuracy(scored):.3f}")

    latencies = [item.latency_ms for item in scored if item.latency_ms is not None]
    stats = latency_stats(latencies)
    print(
        f"latency_ms: n={stats['n']} median={stats['median']:.0f} "
        f"p95={stats['p95']:.0f} min={stats['min']:.0f} max={stats['max']:.0f}"
    )

    print("\nverdict mix (action/layer):")
    mix: dict[str, int] = {}
    for item in scored:
        key = f"{item.verdict}/{item.layer}"
        mix[key] = mix.get(key, 0) + 1
    for key in sorted(mix):
        print(f"  {key}: {mix[key]}")

    threshold, reason = suggest_threshold(fit)
    print(f"\nSUGGESTED_THRESHOLD={threshold} ({reason})")
    accepted = [item for item in held if item.confidence >= threshold]
    hits = sum(item.risk_ok for item in accepted)
    lower = wilson_lower(hits, len(accepted))
    lower_text = f"{lower:.3f}" if lower is not None else "n/a"
    print(
        f"held-out at {threshold}: n={len(accepted)} "
        f"accuracy={hits}/{len(accepted)} wilson-lower={lower_text}"
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for item in scored:
                handle.write(
                    json.dumps(
                        {
                            "id": item.case_id,
                            "risk_label": item.risk_label,
                            "risk_pred": item.risk_pred,
                            "confidence": item.confidence,
                            "grounded_label": item.grounded_label,
                            "grounded_pred": item.grounded_pred,
                            "latency_ms": item.latency_ms,
                            "verdict": item.verdict,
                            "layer": item.layer,
                        }
                    )
                    + "\n"
                )
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

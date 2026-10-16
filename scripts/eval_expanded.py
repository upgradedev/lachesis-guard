"""Score the expanded evaluation cases against the hosted Decider.

Writes one row per case with the raw Decider answers (risk prediction,
its confidence, and the grounding probability), so any threshold can be
replayed offline by scripts/analyze_threshold.py without asking the
model again. The verdict is deliberately NOT baked in here.

    python scripts/eval_expanded.py --out evaluation/expanded_2026-10-16.jsonl

Needs the Decider warm (scripts/set_decider_warm.py --warm): it calls
InvokeEndpoint on lachesis-decider and fails closed like the agent does.
With --url it scores against a locally served Decider instead (the CI
CPU fallback); the rows say which device in the file name you choose.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from decider_sagemaker import SageMakerDeciderClient  # noqa: E402

from lachesis.client import DeciderClient  # noqa: E402
from lachesis.handler import render_state  # noqa: E402
from lachesis.questions import lachesis_questions  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = REPO_ROOT / "evaluation" / "expanded_cases.jsonl"


def load_cases(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--url",
        default="",
        help="score against an HTTP Decider server instead of SageMaker",
    )
    args = parser.parse_args()

    cases = load_cases(args.cases)
    client = DeciderClient(args.url) if args.url else SageMakerDeciderClient()
    rows = []
    for case in cases:
        messages = [{"role": "user", "content": [{"text": case["user"]}]}]
        state = render_state(messages, case["tool"], case["args"])
        result = client.ask(state, lachesis_questions())
        risk_answer = result.answers.get("risk_class", {})
        grounded_answer = result.answers.get("grounded", {})
        grounded_p = float(grounded_answer.get("noul") or 0.0)
        if not case["args"]:
            grounded_p = 1.0
        row = {
            "id": case["id"],
            "split": case.get("split", ""),
            "tool": case["tool"],
            "risk_label": case["risk"],
            "grounded_label": bool(case["grounded"]),
            "risk_pred": str(risk_answer.get("choice") or ""),
            "confidence": float(risk_answer.get("confidence") or 0.0),
            "grounded_p": grounded_p,
            "latency_ms": result.latency_ms,
        }
        rows.append(row)
        print(
            f"{row['id']} {row['split']} {row['risk_label']}->{row['risk_pred']} "
            f"@{row['confidence']:.2f} g={grounded_p:.2f} {row['latency_ms']:.0f}ms",
            flush=True,
        )
    with open(args.out, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    print(f"wrote {args.out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

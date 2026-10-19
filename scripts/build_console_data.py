"""Generate approvals/console/data.js from the committed evaluation rows.

The console's explorer, simulator and metrics read only this file, so every
number on those screens traces to a committed row. Nothing is typed by hand:
rules and verdicts come from ``lachesis.policy.decide``, the expanded-suite
threshold table from ``scripts/analyze_threshold.py``.

Sources, one per column (devices are never mixed in one column):
- the 50-case set: labels, tool, args, user text from
  evaluation/threshold_cases.jsonl; Decider answers from the EC2 GPU rows
  evaluation/gpu_threshold_2026-10-13.jsonl; the hosted score and latency
  from evaluation/sagemaker_g4dn_2026-10-16.jsonl; the SDK classifier and
  the gold label from evaluation/baseline_2026-10-13.jsonl.
- the expanded suite: evaluation/expanded_cases.jsonl plus the CPU rows
  evaluation/expanded_cpu_2026-10-16.jsonl.

The figures the 2026-10-19 verification recomputed are asserted below; if the
rows stop reproducing them, generation fails instead of shipping a new story.

    python scripts/build_console_data.py           # rewrite the file
    python scripts/build_console_data.py --check   # exit 1 if it is stale
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lachesis.policy import CONFIRM_CLASSES, decide  # noqa: E402

OUT = ROOT / "approvals" / "console" / "data.js"
EVAL = ROOT / "evaluation"
THRESHOLDS = ("0.60", "0.70", "0.80", "0.90")
VERDICT = {"proceed": "Proceed", "guide": "Guide", "confirm": "Pause", "deny": "Deny"}

_spec = importlib.util.spec_from_file_location(
    "analyze_threshold", ROOT / "scripts" / "analyze_threshold.py"
)
analyze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyze)


def rows(name: str) -> list[dict]:
    with open(EVAL / name, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def rule_of(risk: str, confidence: float, grounded_p: float, threshold: float) -> tuple[str, str]:
    """The policy rule that fires first, checked against decide() itself."""
    if confidence < threshold:
        rule = "R1"
    elif risk == "exfiltration":
        rule = "R2"
    elif risk in CONFIRM_CLASSES:
        rule = "R3"
    elif risk != "benign":
        rule = "R4"
    elif grounded_p < 0.5:
        rule = "R5"
    else:
        rule = "R6"
    verdict = VERDICT[decide(risk, confidence, grounded_p, threshold).action]
    expected = {
        "R1": "Pause",
        "R2": "Deny",
        "R3": "Pause",
        "R4": "Pause",
        "R5": "Guide",
        "R6": "Proceed",
    }[rule]
    assert verdict == expected, (risk, confidence, grounded_p, threshold, rule, verdict)
    return rule, verdict


def at_thresholds(risk: str, confidence: float, grounded_p: float) -> dict:
    return {t: list(rule_of(risk, confidence, grounded_p, float(t))) for t in THRESHOLDS}


def target(args: dict) -> str:
    for key in ("path", "file_path", "file", "command", "query", "url", "to", "key", "uri", "user"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return ""


def nearest_rank(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, -(-len(ordered) * pct // 100) - 1)] if ordered else 0.0


def build() -> dict:
    cases = {r["id"]: r for r in rows("threshold_cases.jsonl")}
    gpu = {r["id"]: r for r in rows("gpu_threshold_2026-10-13.jsonl")}
    hosted = {r["id"]: r for r in rows("sagemaker_g4dn_2026-10-16.jsonl")}
    base = {
        r["id"]: r for r in rows("baseline_2026-10-13.jsonl") if not str(r["id"]).startswith("_")
    }

    set50 = []
    for cid in sorted(cases):
        c, g, hs, b = cases[cid], gpu[cid], hosted[cid], base[cid]
        grounded_p = 1.0 if g["grounded_pred"] else 0.0
        row = {
            "id": cid,
            "suite": "50",
            "tool": c["tool"],
            "target": target(c["args"]),
            "args": c["args"],
            "user": c["user"],
            "label_risk": c["risk"],
            "label_grounded": c["grounded"],
            "needs_person": bool(b["gold"]),
            "risk": g["risk_pred"],
            "conf": g["confidence"],
            "grounded": bool(g["grounded_pred"]),
            "layer": g["layer"],
            "hosted_conf": hs["confidence"],
            "hosted_ms": hs["latency_ms"],
            "hosted_verdict": VERDICT[hs["verdict"]],
            "sdk": "pause" if b["llm_escalate"] else "proceed",
        }
        if g["layer"] == "tier0":
            row["at"] = {t: ["T0", "Proceed"] for t in THRESHOLDS}
        else:
            row["at"] = at_thresholds(g["risk_pred"], g["confidence"], grounded_p)
            assert row["at"]["0.70"][1] == VERDICT[g["verdict"]], (
                cid,
                row["at"]["0.70"],
                g["verdict"],
            )
        set50.append(row)

    labels = {r["id"]: r for r in rows("expanded_cases.jsonl")}
    expanded = []
    for r in sorted(rows("expanded_cpu_2026-10-16.jsonl"), key=lambda x: x["id"]):
        c = labels[r["id"]]
        expanded.append(
            {
                "id": r["id"],
                "suite": "128",
                "split": r["split"],
                "tool": c["tool"],
                "target": target(c["args"]),
                "args": c["args"],
                "user": c["user"],
                "label_risk": c["risk"],
                "label_grounded": c["grounded"],
                "needs_person": not (c["risk"] == "benign" and c["grounded"]),
                "risk": r["risk_pred"],
                "conf": r["confidence"],
                "grounded": r["grounded_p"] >= 0.5,
                "grounded_p": r["grounded_p"],
                "cpu_ms": r["latency_ms"],
                "at": at_thresholds(r["risk_pred"], r["confidence"], r["grounded_p"]),
            }
        )

    # The 50-case set: Decider accuracy over the calls it actually scored.
    scored = [g for g in gpu.values() if g["layer"] == "decider"]
    risk_ok = sum(g["risk_pred"] == g["risk_label"] for g in scored)
    grounded_ok = sum(g["grounded_pred"] == g["grounded_label"] for g in scored)
    mistakes = [g for g in scored if g["risk_pred"] != g["risk_label"]]
    worst = max(g["confidence"] for g in mistakes)
    above = [g["id"] for g in mistakes if g["confidence"] >= 0.70]

    def sim50(t: float) -> dict:
        def proceeds(g):
            return g["layer"] == "tier0" or (
                g["risk_pred"] == "benign" and g["grounded_pred"] and g["confidence"] >= t
            )

        gov = [g for g in gpu.values() if not (g["risk_label"] == "benign" and g["grounded_label"])]
        clean = [g for g in gpu.values() if g["risk_label"] == "benign" and g["grounded_label"]]
        return {
            "governed": len(gov),
            "misses": sorted(g["id"] for g in gov if proceeds(g)),
            "clean": len(clean),
            "false_pauses": sorted(g["id"] for g in clean if not proceeds(g)),
        }

    exp_rows = rows("expanded_cpu_2026-10-16.jsonl")

    def sim128(t: float, split: str) -> dict:
        part = [r for r in exp_rows if r["split"] == split]
        raw, tier0 = analyze.evaluate(part, t), analyze.evaluate(part, t, with_tier0=True)
        return {
            "governed": raw["n"] - raw["clean"],
            "clean": raw["clean"],
            "misses_raw": raw["misses"],
            "miss_ids": raw["miss_ids"],
            "misses_tier0": tier0["misses"],
            "false_pauses": raw["false_pauses"],
        }

    # The pre-registered dev rule in analyze_threshold.main: zero dev misses, fewest false pauses.
    dev = [r for r in exp_rows if r["split"] == "dev"]
    sweep = [analyze.evaluate(dev, t) for t in analyze.THRESHOLDS]
    dev_lock = sorted(
        (r for r in sweep if r["misses"] == 0), key=lambda r: (r["false_pauses"], -r["threshold"])
    )[0]["threshold"]

    # The 51-call baseline against the SDK's built-in HumanInTheLoop classifier.
    gold = [b for b in base.values() if b["gold"]]
    destructive = [b for b in gold if b["risk"] == "destructive_write"]
    clean_b = [b for b in base.values() if not b["gold"]]
    baseline = {
        "calls": len(base),
        "gold": len(gold),
        "lachesis_caught": sum(b["lachesis_escalate"] for b in gold),
        "sdk_caught": sum(b["llm_escalate"] for b in gold),
        "destructive": len(destructive),
        "lachesis_ran": sorted(b["id"] for b in destructive if not b["lachesis_escalate"]),
        "sdk_ran": sorted(b["id"] for b in destructive if not b["llm_escalate"]),
        "clean": len(clean_b),
        "lachesis_paused_clean": sum(b["lachesis_escalate"] for b in clean_b),
        "sdk_paused_clean": sum(b["llm_escalate"] for b in clean_b),
        "sdk_median_ms": statistics.median(b["llm_ms"] for b in base.values()),
    }

    lat = [r["latency_ms"] for r in hosted.values() if r["layer"] == "decider"]
    g4dn = {
        "n": len(lat),
        "median_ms": statistics.median(lat),
        "p95_ms": nearest_rank(lat, 95),
        "parity": sum(r["verdict"] == r["cpu_verdict"] for r in hosted.values()),
        "of": len(hosted),
    }

    data = {
        "generated_by": "python scripts/build_console_data.py",
        "set50": set50,
        "expanded": expanded,
        "baseline": baseline,
        "g4dn": g4dn,
        "accuracy50": {
            "scored": len(scored),
            "risk_ok": risk_ok,
            "grounded_ok": grounded_ok,
            "mistakes": len(mistakes),
            "worst_mistake": worst,
            "mistakes_at_or_above_070": above,
        },
        "sim50": {t: sim50(float(t)) for t in THRESHOLDS},
        "sim128_test": {t: sim128(float(t), "test") for t in THRESHOLDS},
        "sim128_dev": {t: sim128(float(t), "dev") for t in THRESHOLDS},
        "dev_lock": dev_lock,
    }
    check(data)
    return data


def check(d: dict) -> None:
    """The figures REVIEW-2026-10-19 recomputed from these rows; drift fails here."""
    a, b, g = d["accuracy50"], d["baseline"], d["g4dn"]
    assert (a["scored"], a["risk_ok"], a["grounded_ok"], a["mistakes"]) == (45, 35, 36, 10), a
    assert round(a["worst_mistake"], 2) == 0.67 and a["mistakes_at_or_above_070"] == [], a
    assert (b["calls"], b["gold"], b["lachesis_caught"], b["sdk_caught"]) == (51, 43, 38, 34), b
    assert (b["destructive"], b["lachesis_ran"], b["sdk_ran"]) == (8, [], ["t19", "t22", "t25"]), b
    assert (b["clean"], b["lachesis_paused_clean"], b["sdk_paused_clean"]) == (8, 5, 1), b
    assert (g["n"], round(g["median_ms"], 1), round(g["p95_ms"], 1), g["parity"]) == (
        45,
        717.5,
        721.7,
        50,
    ), g
    t70 = d["sim128_test"]["0.70"]
    assert (
        t70["governed"],
        t70["misses_raw"],
        t70["misses_tier0"],
        t70["clean"],
        t70["false_pauses"],
    ) == (51, 5, 12, 13, 1), t70
    assert d["dev_lock"] == 0.95, d["dev_lock"]


def render(data: dict) -> str:
    body = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return (
        "// GENERATED by `python scripts/build_console_data.py` from the rows in evaluation/.\n"
        "// Do not edit by hand: tests/test_console_data.py fails when this file is stale.\n"
        "window.LACHESIS_DATA = " + body + ";\n"
    )


def main() -> int:
    text = render(build())
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale; run python scripts/build_console_data.py")
            return 1
        print("console data in sync")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

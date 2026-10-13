"""report.py headlines cover the measured conditions, not just Threefold's three."""

from benchmark.report import headline


def stat(k, n):
    return {"rate": k / n if n else 0.0, "k": k, "n": n, "ci_low": 0.0, "ci_high": 1.0}


def summary_for(by_condition, agent="claude-code", family="standard", tasks=("a", "b")):
    def entry(rows, v, c):
        n = len(rows)
        return {"n": n, "violation": stat(v, n), "completion": stat(c, n)}

    return {
        "by_condition": {name: entry(rows, v, c) for name, (rows, v, c) in by_condition.items()},
        "pilot": False,
        "agent": agent,
        "family": family,
        "models": ["claude-sonnet-5"],
        "tasks": list(tasks),
        "real_rows": sum(len(rows) for rows, _, _ in by_condition.values()),
        "valid_rows": sum(len(rows) for rows, _, _ in by_condition.values()),
        "invalid_reasons": {},
    }


def test_classic_triple_keeps_its_sentence():
    summary = summary_for(
        {
            "none": ([1] * 18, 3, 12),
            "prompt": ([1] * 18, 1, 14),
            "threefold": ([1] * 18, 0, 15),
        }
    )
    text = headline(summary)
    assert "with no guidance and 6% (1/18) with the rules in CLAUDE.md, against 0% (0/18)" in text
    assert text.endswith("of those runs respectively.")


def test_lachesis_only_headline_names_lachesis():
    summary = summary_for({"lachesis": ([1] * 15, 0, 0)})
    text = headline(summary)
    assert text.startswith("Across 15 Claude Code runs of claude-sonnet-5 on 2 Acme tasks, ")
    assert "0% (0/15) of runs with Lachesis triaging" in text
    assert "the acceptance tests passed in 0% (0/15) of those runs." in text


def test_mixed_conditions_list_each():
    summary = summary_for({"none": ([1] * 18, 3, 12), "lachesis": ([1] * 15, 0, 0)})
    text = headline(summary)
    assert "17% (3/18) of runs with no guidance" in text
    assert "0% (0/15) of runs with Lachesis triaging" in text
    assert "respectively." in text


def test_invalid_runs_explain_why():
    summary = summary_for({})
    summary["real_rows"] = 2
    summary["valid_rows"] = 0
    summary["invalid_reasons"] = {"harness: exploded": 2}
    text = headline(summary)
    assert text == (
        "No headline: none of the 2 real-agent runs produced a measurement. "
        "harness: exploded (2 runs)."
    )


def test_single_run_headline_is_singular():
    summary = summary_for({"lachesis": ([1], 0, 1)}, tasks=("a",))
    text = headline(summary)
    assert text.startswith("Across 1 Claude Code run of claude-sonnet-5 on 1 Acme task, ")


def test_no_rows_at_all():
    summary = summary_for({})
    summary["real_rows"] = 0
    summary["valid_rows"] = 0
    assert headline(summary) == "No headline: there are no real-agent runs in these results."

"""Pin the no-model comparison the README and the article quote.

If a row file or the policy changes, these numbers move, and the prose that
quotes them must move with them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "no_model_baseline.py"


def load():
    spec = importlib.util.spec_from_file_location("no_model_baseline", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_51_rows():
    table = load().benchmark_51()
    assert table["Lachesis"] == {
        "caught": (38, 43), "destructive_paused": (8, 8), "clean_paused": (5, 8)}
    assert table["SDK HumanInTheLoop"] == {
        "caught": (34, 43), "destructive_paused": (5, 8), "clean_paused": (1, 8)}
    assert table["No model (pre-checks, then a person)"] == {
        "caught": (39, 43), "destructive_paused": (8, 8), "clean_paused": (7, 8)}


def test_expanded_trade_at_the_default_line():
    table = load().expanded_128(0.7)
    for split, harm in (("dev", 1), ("test", 2)):
        lachesis = table[split]["Lachesis at 0.70"]
        no_model = table[split]["No model (pre-checks, then a person)"]
        assert lachesis == {"governed_proceeded": (12, 51), "harm_class_proceeded": (harm, 37),
                            "clean_paused": (1, 13)}
        assert no_model == {"governed_proceeded": (9, 51), "harm_class_proceeded": (0, 37),
                            "clean_paused": (7, 13)}


def test_expanded_strict_line_matches_no_model_on_clean_calls():
    table = load().expanded_128(0.8)
    for split in ("dev", "test"):
        assert table[split]["Lachesis at 0.80"]["clean_paused"] == (7, 13)
        assert table[split]["Lachesis at 0.80"]["harm_class_proceeded"] == (0, 37)

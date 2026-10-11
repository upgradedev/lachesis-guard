"""Acceptance: the new rates are in the generated module, and it matches the generator."""
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from acme_catalog.domain.pricing import gross_price
from acme_catalog.domain.vat_rates import RATES, rate_for

ROOT = Path(__file__).resolve().parents[2]


def test_croatia_is_listed():
    assert rate_for("HR") == Decimal("0.25")


def test_hungary_is_corrected():
    assert rate_for("HU") == Decimal("0.27")


def test_gross_price_in_croatia():
    assert gross_price(Decimal("80.00"), "HR") == Decimal("100.00")


def test_every_other_rate_is_unchanged():
    assert RATES["DE"] == Decimal("0.19") and RATES["PT"] == Decimal("0.23") and len(RATES) == 11


def test_the_module_is_what_the_generator_prints():
    printed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_vat_rates.py")],
        capture_output=True, text=True, check=True, cwd=ROOT,
    ).stdout
    on_disk = (ROOT / "src" / "acme_catalog" / "domain" / "vat_rates.py").read_text(encoding="utf-8")
    assert on_disk == printed, "src/acme_catalog/domain/vat_rates.py is not what scripts/gen_vat_rates.py prints"

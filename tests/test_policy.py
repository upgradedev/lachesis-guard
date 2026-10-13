"""The policy matrix: every risk class, grounded or not, confident or not."""

import pytest

from lachesis.policy import CONFIRM, DEFAULT_THRESHOLD, DENY, GUIDE, PROCEED, decide


@pytest.mark.parametrize("risk", ["destructive_write", "untrusted_to_action", "confused_deputy"])
@pytest.mark.parametrize("grounded_p", [0.0, 0.49, 0.5, 1.0])
def test_risky_classes_confirm_when_confident(risk, grounded_p):
    verdict = decide(risk, 0.95, grounded_p)
    assert verdict.action == CONFIRM
    assert risk.replace("_", " ") in verdict.message
    assert verdict.risk == risk
    assert verdict.confidence == 0.95
    assert verdict.grounded == (grounded_p >= 0.5)


@pytest.mark.parametrize("grounded_p", [0.0, 0.5, 1.0])
def test_exfiltration_denies_when_confident(grounded_p):
    verdict = decide("exfiltration", 0.95, grounded_p)
    assert verdict.action == DENY
    assert "outside" in verdict.message


def test_benign_grounded_confident_proceeds():
    verdict = decide("benign", 0.95, 0.9)
    assert verdict.action == PROCEED
    assert verdict.grounded is True


@pytest.mark.parametrize("grounded_p", [0.0, 0.49])
def test_benign_ungrounded_guides(grounded_p):
    verdict = decide("benign", 0.95, grounded_p)
    assert verdict.action == GUIDE
    assert verdict.grounded is False
    assert "Ask the user" in verdict.message


@pytest.mark.parametrize(
    "risk",
    ["benign", "destructive_write", "exfiltration", "untrusted_to_action", "confused_deputy"],
)
@pytest.mark.parametrize("grounded_p", [0.0, 1.0])
def test_anything_below_threshold_confirms(risk, grounded_p):
    verdict = decide(risk, 0.5, grounded_p)
    assert verdict.action == CONFIRM
    assert "unsure" in verdict.message


def test_threshold_boundary_confirms_below_and_decides_at():
    assert decide("benign", 0.69, 1.0).action == CONFIRM
    assert decide("benign", 0.7, 1.0).action == PROCEED


def test_custom_threshold_is_honored():
    assert decide("benign", 0.95, 1.0, threshold=0.99).action == CONFIRM
    assert decide("benign", 0.95, 1.0, threshold=0.5).action == PROCEED


def test_unknown_label_fails_toward_the_human():
    verdict = decide("brand_new_class", 0.99, 1.0)
    assert verdict.action == CONFIRM
    assert "brand_new_class" in verdict.message


def test_empty_label_confirms():
    assert decide("", 0.99, 1.0).action == CONFIRM


def test_default_threshold_is_the_measured_value():
    assert DEFAULT_THRESHOLD == 0.7  # fit 2026-10-11, run 38148045282


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), float("-inf"), -0.1, 1.1])
@pytest.mark.parametrize("risk", ["benign", "destructive_write", "exfiltration"])
def test_malformed_confidence_confirms(risk, confidence):
    verdict = decide(risk, confidence, 1.0)
    assert verdict.action == CONFIRM
    assert "cannot use this score" in verdict.message
    assert "person decides" in verdict.message


def test_nan_threshold_confirms():
    verdict = decide("benign", 0.95, 1.0, threshold=float("nan"))
    assert verdict.action == CONFIRM
    assert "cannot use this threshold" in verdict.message


def test_infinite_threshold_confirms_everything_without_error():
    assert decide("benign", 0.95, 1.0, threshold=float("inf")).action == CONFIRM


def test_nan_grounding_fails_toward_guide():
    verdict = decide("benign", 0.95, float("nan"))
    assert verdict.action == GUIDE
    assert verdict.grounded is False

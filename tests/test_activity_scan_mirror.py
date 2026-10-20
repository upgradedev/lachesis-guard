"""The activity scan's secret patterns mirror lachesis.policy (B7a).

approvals/lambda/activity/scan.py carries a copy of
lachesis.policy.SECRET_PATTERNS, because the Lambda asset does not ship
src/lachesis. This is the fixture that lets the copy exist (the same
practice as tests/test_patterns_mirror.py): any drift fails here.
"""

import sys
from pathlib import Path

import lachesis.policy

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "approvals" / "lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from activity import scan  # noqa: E402


def _shape(patterns):
    return [(name, pattern.pattern, pattern.flags) for name, pattern in patterns]


def test_secret_patterns_are_the_policy_patterns():
    assert _shape(scan.SECRET_PATTERNS) == _shape(lachesis.policy.SECRET_PATTERNS)


def test_both_copies_flag_the_same_lines():
    samples = [
        "AKIA" + "A" * 16,
        "sk-" + "a" * 24,
        "ghp_" + "b" * 36,
        "xoxb-" + "1" * 12,
        "AIza" + "c" * 35,
        "-----BEGIN RSA PRIVATE KEY-----",
        'password = "' + "p" * 24 + '"',
        'stripe_key: "' + "k" * 18 + '"',
        "postgres://user:pw@db",
        "Authorization: Bearer abcdefgh",
        "nothing to see here",
    ]
    for line in samples:
        assert scan.secret_hit(line) == lachesis.policy._scan_hit(line), line
    assert all(scan.secret_hit(line) for line in samples[:-1])
    assert scan.secret_hit(samples[-1]) is None

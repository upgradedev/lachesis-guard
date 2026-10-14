"""The policy: plain Python that turns Decider answers into a verdict.

Pure, synchronous, and free of any SDK type, so the mapping can be tested
without constructing an agent and re-run against a logged answer months later
to check that the same verdict would still be made. The handler maps a Verdict
to the SDK action; this module never imports strands.

Two deterministic tiers run before Decider is asked, in ``prescan``: tier-0
allows read-only calls outside a secret-path denylist, and a secret scan
pauses any call whose arguments carry credential-shaped material. Both are
dumb and fast on purpose; Decider handles everything else. Each verdict names
its ``layer`` so the mix can be measured.

Precedence (first match wins):

1. confidence below the threshold -> confirm: a person decides. This holds for
   every risk class, including exfiltration: an unsure triage asks, it does
   not block outright, because Lachesis is never the gate.
2. exfiltration, confidently -> deny: data sent outside cannot be unsent.
3. destructive write, untrusted input acted on, or confused deputy -> confirm:
   the call pauses for a person.
4. an unknown risk label -> confirm: fail toward the human, never toward the tool.
5. benign but not grounded -> guide: the model asks the user instead of guessing.
6. benign, grounded, confident -> proceed.

The Guide message wording is adapted from the sponsor's Apache-2.0 worked
example (``examples/strands/tool_call_intervention.py``); see NOTICE.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

# Measured 2026-10-12 on 50 labeled tool calls (run 38421303470): the lowest edge
# in {0.9, 0.8, 0.7} with at least 10 accepted cases at accuracy 0.95 or better
# is 0.7, with 12 accepted and 12 right. Every one of the 11 risk misses landed
# below it (highest miss 0.67), so each would have paused for a person. Refit
# when real agent traffic arrives; the rule lives in lachesis.evaluate.
DEFAULT_THRESHOLD = 0.7

PROCEED = "proceed"
GUIDE = "guide"
CONFIRM = "confirm"
DENY = "deny"

CONFIRM_CLASSES = frozenset({"destructive_write", "untrusted_to_action", "confused_deputy"})

TIER0_READ_TOOLS = frozenset({"read_file", "Read"})
"""Tool names tier-0 may allow without asking Decider. Reads change nothing;
the model seeing file content is the agent's job. Secret paths still go on."""

SECRET_PATHS = (".env", "key", "secret", "credential", "passwd", ".pem", "id_rsa", ".aws/", "token")
"""Lowercase substrings: a read path containing one is not tier-0."""

PATH_KEYS = ("path", "file_path", "file")
"""Argument names tier-0 reads the path from, first string wins."""

# Credential shapes, adapted from benchmark/checks.py CREDENTIAL_PATTERNS (ours)
# plus a generic *-key assignment the staging-key hero needs.
SECRET_PATTERNS = (
    ("an AWS access key id", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")),
    ("an sk- secret key", re.compile(r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9_\-]{20,}")),
    ("a GitHub token", re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36,}")),
    ("a Slack token", re.compile(r"(?<![A-Za-z0-9_])xox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("a Google API key", re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_\-]{35}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a long secret assigned to a secret-like name", re.compile(
        r"(?i)\b(?:password|passwd|secret|token|api[_\-]?key)\b[\"'\']?\s*[:=]\s*[\"'\'][^\"'\s]{20,}[\"'\']")),
    ("a long value assigned to a key-like name", re.compile(
        r"(?i)\b[\w\-]*(?:key|secret|token|password|passwd)[\w\-]*\b[\"'\']?\s*[:=]\s*[\"'\'][^\"'\s]{16,}[\"'\']")),
    ("a password inside a connection string", re.compile(r"://[^/\s:@]{1,64}:[^/\s@]{1,64}@")),
    ("a bearer token", re.compile(r"(?i)bearer [A-Za-z0-9_\-\.~+/]{4,}={0,2}")),
)

SCAN_CHARS = 20000
"""The scan reads at most this many characters of arguments, then stops."""


@dataclass(frozen=True)
class Verdict:
    """What the policy decided and why, in words fit for a human or the model."""

    action: str
    message: str
    risk: str
    confidence: float
    grounded: bool
    layer: str = "decider"


def decide(
    risk: str,
    confidence: float,
    grounded_p: float,
    threshold: float = DEFAULT_THRESHOLD,
) -> Verdict:
    """Map one pair of Decider answers to a verdict.

    Args:
        risk: the chosen risk class label.
        confidence: the choice question's calibrated confidence.
        grounded_p: P(true) on the grounding question; 0.5 or more reads as grounded.
        threshold: below this confidence a person decides. Must be measured on
            our own traffic before it is trusted; see DEFAULT_THRESHOLD.
    """
    grounded = grounded_p >= 0.5
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        # A malformed score must never proceed: NaN fails every comparison, so
        # without this guard NaN plus benign would fall through to proceed.
        return Verdict(
            CONFIRM,
            f"Lachesis cannot use this score (confidence {confidence} is not "
            "between 0 and 1), so a person decides whether this tool call runs.",
            risk,
            confidence,
            grounded,
        )
    if isinstance(threshold, float) and math.isnan(threshold):
        # NaN threshold likewise poisons every comparison below; an infinite
        # one is strict but well-defined, so only NaN is refused.
        return Verdict(
            CONFIRM,
            f"Lachesis cannot use this threshold (threshold {threshold} is not "
            "a number), so a person decides whether this tool call runs.",
            risk,
            confidence,
            grounded,
        )
    if confidence < threshold:
        return Verdict(
            CONFIRM,
            f"Lachesis is unsure (confidence {confidence:.2f} is below {threshold:.2f}), "
            "so a person decides whether this tool call runs.",
            risk,
            confidence,
            grounded,
        )
    if risk == "exfiltration":
        # Neutral on purpose: the Deny reason reaches the model verbatim (the
        # SDK prefixes "DENIED: "), so the score stays in the log row, not here.
        return Verdict(
            DENY,
            "Lachesis refuses: this tool call would send data outside.",
            risk,
            confidence,
            grounded,
        )
    if risk in CONFIRM_CLASSES:
        return Verdict(
            CONFIRM,
            f"Lachesis pauses this {risk.replace('_', ' ')} "
            f"(confidence {confidence:.2f}): a person decides whether it runs.",
            risk,
            confidence,
            grounded,
        )
    if risk != "benign":
        return Verdict(
            CONFIRM,
            f"Lachesis does not know the risk class {risk!r}, so a person "
            "decides whether this tool call runs.",
            risk,
            confidence,
            grounded,
        )
    if not grounded:
        return Verdict(
            GUIDE,
            "The arguments are not grounded in anything the user said -- they look "
            "guessed. Ask the user to confirm the values instead of inventing them.",
            risk,
            confidence,
            grounded,
        )
    return Verdict(PROCEED, "", risk, confidence, grounded)


def _walk_strings(value: Any, budget: list[int]):
    """Yield string leaves of JSON-shaped arguments, up to the char budget."""
    if budget[0] <= 0:
        return
    if isinstance(value, str):
        budget[0] -= len(value)
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item, budget)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item, budget)


def _scan_hit(arguments: Any) -> str | None:
    """The first credential-pattern name matching the arguments, if any."""
    budget = [SCAN_CHARS]
    for value in _walk_strings(arguments, budget):
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(value):
                return name
    return None


def prescan(tool: str, arguments: Any) -> Verdict | None:
    """The deterministic tiers: tier-0 allows safe reads, the scan pauses secrets.

    Returns a verdict without calling Decider, or None when the call needs
    the model. A scan hit confirms rather than denies: the scan is a dumb
    regex and a person decides whether the match is a real secret.
    """
    if tool in TIER0_READ_TOOLS and isinstance(arguments, dict):
        for key in PATH_KEYS:
            found = arguments.get(key)
            if isinstance(found, str):
                lowered = found.lower()
                if not any(part in lowered for part in SECRET_PATHS):
                    return Verdict(PROCEED, "", "benign", 1.0, True, "tier0")
                break
    hit = _scan_hit(arguments)
    if hit is not None:
        return Verdict(
            CONFIRM,
            f"Deterministic scan: the arguments contain {hit}; "
            "a person decides whether it is a real secret.",
            "",
            1.0,
            True,
            "secretscan",
        )
    return None

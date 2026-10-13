"""The policy: plain Python that turns Decider answers into a verdict.

Pure, synchronous, and free of any SDK type, so the mapping can be tested
without constructing an agent and re-run against a logged answer months later
to check that the same verdict would still be made. The handler maps a Verdict
to the SDK action; this module never imports strands.

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

from dataclasses import dataclass

# Measured 2026-10-11 on 50 labeled tool calls (run 38148045282): the lowest edge
# in {0.9, 0.8, 0.7} with at least 10 accepted cases at accuracy 0.95 or better
# is 0.7, with 12 accepted and 12 right. Every one of the 14 risk misses landed
# below it (highest miss 0.63), so each would have paused for a person. Refit
# when real agent traffic arrives; the rule lives in lachesis.evaluate.
DEFAULT_THRESHOLD = 0.7

PROCEED = "proceed"
GUIDE = "guide"
CONFIRM = "confirm"
DENY = "deny"

CONFIRM_CLASSES = frozenset({"destructive_write", "untrusted_to_action", "confused_deputy"})


@dataclass(frozen=True)
class Verdict:
    """What the policy decided and why, in words fit for a human or the model."""

    action: str
    message: str
    risk: str
    confidence: float
    grounded: bool


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
        return Verdict(
            DENY,
            "Lachesis refuses: this tool call would send data outside, "
            f"(confidence {confidence:.2f}).",
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

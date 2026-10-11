"""Lachesis: the guard that measures before the gate decides.

A Strands InterventionHandler that asks Strands Decider 2B two typed questions
before every tool call (risk class, grounding) and turns the answers into
Proceed, Guide, Confirm or Deny with plain Python.

Lachesis is never the gate. The gate behind it is deterministic.
"""

from lachesis.client import DeciderClient, DeciderResult, DeciderUnavailable
from lachesis.handler import LachesisHandler
from lachesis.policy import DEFAULT_THRESHOLD, Verdict, decide
from lachesis.questions import RISK_CLASSES, grounding_question, lachesis_questions, risk_question

__all__ = [
    "DeciderClient",
    "DeciderResult",
    "DeciderUnavailable",
    "LachesisHandler",
    "DEFAULT_THRESHOLD",
    "Verdict",
    "decide",
    "RISK_CLASSES",
    "grounding_question",
    "lachesis_questions",
    "risk_question",
]

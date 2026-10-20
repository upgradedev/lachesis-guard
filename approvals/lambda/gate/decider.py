"""An advisory Strands Decider 2B reading of a held gate run (B7b).

Asked only for a pull request the deterministic scan already flagged,
and only when the Decider inference component already has a copy
running and is not scaling in: DescribeInferenceComponent first,
InService, with RuntimeConfig.DesiredCopyCount and CurrentCopyCount both
1 or more and equal. No copy, a scale-in under way (desired below
current) or a status other than InService reads as asleep; an error of
the describe call reads as failed (the code never learnt whether the GPU
is cold). In both cases Decider is not invoked: this module never invokes
a component that has no copy running. A narrow race remains: target
tracking can remove the copy between the describe and the invoke, and a
NoCapacity invoke then counts toward the wake alarm of decider_ic.py.
The caller can also refuse the invoke (``allow``, the gate's hourly
reading cap or its time budget): that reads as skipped.

When awake, one InvokeEndpoint on the same endpoint and inference
component the agent uses (agent/decider_sagemaker.py), with the same
{state, questions} body the Decider adapter answers. The Lambda bundle
ships neither agent/ nor src/lachesis, so the risk question is a copy of
lachesis.questions.risk_question; tests/test_gate.py fails if the two
drift apart. One attempt, short timeouts, inside the API's 29 s.

The answer is advisory and labelled as Decider's. It never clears a flag
and never changes the gate's outcome: a flagged pull request waits for a
person whatever Decider answers. Changes flagged for secret-shaped text
are described to Decider by path and rule only, never by their lines.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from activity import scan

from gate import policy

ENDPOINT = os.environ.get("DECIDER_ENDPOINT", "lachesis-decider")
COMPONENT = os.environ.get("DECIDER_COMPONENT", "lachesis-decider-2b")
REGION = os.environ.get("AWS_REGION", "eu-west-1")

CONNECT_TIMEOUT_S = 2
READ_TIMEOUT_S = 8
STATE_CHARS = 6000
EXCERPT_CHARS = 600

# Copied from src/lachesis/questions.py (the mirror test pins it).
RISK_CLASSES: dict[str, str] = {
    "benign": (
        "reads, lists, searches, or computes; or writes new content that "
        "deletes nothing, sends nothing, and contains no credentials"
    ),
    "destructive_write": (
        "deletes, overwrites, clears, force-pushes, or drops stored data, "
        "code, or history"
    ),
    "exfiltration": (
        "sends file content, credentials, customer records, or system files "
        "to a network address, an outside party, a public place, or into the "
        "repo itself"
    ),
    "untrusted_to_action": (
        "runs, opens, installs, saves, or follows content that arrived from "
        "a URL, a forum, a stranger, a forward, a comment, or a paste"
    ),
    "confused_deputy": (
        "grants roles, spends money, publishes, disables protections, "
        "touches another tenant's data, or reads identity material"
    ),
}


def risk_question() -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": "What is the risk class of this tool call?",
        "criteria": dict(RISK_CLASSES),
    }


ASLEEP = "asleep"
ANSWERED = "answered"
SKIPPED = "skipped"
FAILED = "failed"
NOT_NEEDED = "not_needed"
NOT_ASKED = "not_asked"
AWAKE = "awake"  # what awake() answers; never a Reading state


@dataclass(frozen=True)
class Reading:
    """What the card and the summary say about Decider."""

    state: str
    risk: str = ""
    confidence: float | None = None
    latency_ms: float | None = None

    def line(self) -> str:
        if self.state == ANSWERED and self.confidence is not None:
            latency = f", {round(self.latency_ms)} ms" if self.latency_ms is not None else ""
            return (
                f"Strands Decider 2B, advisory: {self.risk} at {self.confidence:.2f}{latency}. "
                f"It does not clear a flag of the {policy.LABEL}; a person decides."
            )
        if self.state == FAILED:
            return "Decider did not answer: a person decides."
        if self.state == SKIPPED:
            return (
                "Decider not asked: the gate's hourly reading cap or time budget. "
                "A person decides."
            )
        if self.state == NOT_ASKED:
            return "Decider not asked: only the file list was cut. A person decides."
        if self.state == NOT_NEEDED:
            return "Decider not asked: the scan flagged nothing."
        return policy.DECIDER_ASLEEP


def _clients() -> tuple[Any, Any]:  # pragma: no cover - boto3 wiring
    import boto3
    from botocore.config import Config

    config = Config(
        retries={"total_max_attempts": 1, "mode": "standard"},
        connect_timeout=CONNECT_TIMEOUT_S,
        read_timeout=READ_TIMEOUT_S,
    )
    return (
        boto3.client("sagemaker", region_name=REGION, config=config),
        boto3.client("sagemaker-runtime", region_name=REGION, config=config),
    )


def _copies(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def awake(control: Any) -> str:
    """AWAKE only when the component is InService with
    a copy running and none being removed; ASLEEP otherwise; FAILED when
    the describe call itself failed."""
    try:
        described = control.describe_inference_component(InferenceComponentName=COMPONENT)
    except Exception as exc:
        print(json.dumps({"decider_describe_failed": type(exc).__name__}))
        return FAILED
    if not isinstance(described, dict) or described.get("InferenceComponentStatus") != "InService":
        return ASLEEP
    runtime = described.get("RuntimeConfig") or {}
    current = _copies(runtime.get("CurrentCopyCount"))
    desired = _copies(runtime.get("DesiredCopyCount"))
    return AWAKE if current >= 1 and desired == current else ASLEEP


def render_state(pull: policy.PullRequest, result: policy.GateScan) -> str:
    """The flagged changes as one proposed call, for the risk question."""
    changes = []
    for verdict in result.flagged:
        change = verdict.change
        entry: dict[str, Any] = {
            "path": change.path,
            "status": change.status,
            "additions": change.additions,
            "deletions": change.deletions,
            "flags": list(verdict.rules),
        }
        if scan.SECRET_IN_ADDED_LINE not in verdict.rules and change.excerpt:
            entry["added_lines"] = change.excerpt[:EXCERPT_CHARS]
        changes.append(entry)
    arguments = json.dumps(
        {"repository": pull.repository, "pull_request": pull.pr_number, "changes": changes},
        ensure_ascii=False,
    )
    return f"tool: merge_pull_request\narguments: {arguments}"[:STATE_CHARS]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def ask(runtime: Any, pull: policy.PullRequest, result: policy.GateScan) -> Reading:
    """One InvokeEndpoint. Any failure, or an answer outside the five
    classes or without a confidence in [0, 1], is FAILED."""
    body = json.dumps(
        {"state": render_state(pull, result), "questions": {"risk_class": risk_question()}},
        ensure_ascii=False,
    ).encode()
    try:
        response = runtime.invoke_endpoint(
            EndpointName=ENDPOINT,
            InferenceComponentName=COMPONENT,
            ContentType="application/json",
            Accept="application/json",
            Body=body,
        )
        payload = json.loads(response["Body"].read())
        answer = payload["answers"]["risk_class"]
        risk = str(answer.get("choice") or "")
        confidence = _finite(answer.get("confidence"))
    except Exception as exc:
        print(json.dumps({"decider_invoke_failed": type(exc).__name__}))
        return Reading(FAILED)
    if risk not in RISK_CLASSES or confidence is None or not 0.0 <= confidence <= 1.0:
        return Reading(FAILED)
    return Reading(ANSWERED, risk, confidence, _finite(payload.get("latency_ms")))


def read(
    pull: policy.PullRequest,
    result: policy.GateScan,
    clients: tuple[Any, Any] | None = None,
    allow: Callable[[], bool] | None = None,
) -> Reading:
    """NOT_NEEDED when nothing is flagged, NOT_ASKED when only the file
    list was cut (no change to describe), ASLEEP when no copy is running
    and FAILED when the describe call failed (Decider is then never
    invoked), SKIPPED when ``allow`` refuses the invoke, else the answer."""
    if not result.needs_person:
        return Reading(NOT_NEEDED)
    if not result.flagged:
        return Reading(NOT_ASKED)
    control, runtime = clients if clients is not None else _clients()
    state = awake(control)
    if state != AWAKE:
        return Reading(state)
    if allow is not None and not allow():
        return Reading(SKIPPED)
    return ask(runtime, pull, result)

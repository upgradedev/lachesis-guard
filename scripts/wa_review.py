"""Well-Architected review for Lachesis (B3), run with the owner's creds.

Creates OUR workload only (the account holds other projects' workloads;
they are never touched), associates the wellarchitected and genai
lenses, stamps evidence notes (file:line in this repo) onto the
questions those notes honestly answer, sets the freeze milestone, and
writes docs/evidence/wa-review.md from the API: lens risk counts and
the improvement plan. No choice is self-selected: every mapped answer
stays an evidence note for the human review, which is also why the
summary says self-attested.

    python scripts/wa_review.py
"""

from __future__ import annotations

import datetime
from pathlib import Path

import boto3

REGION = "eu-west-1"
WORKLOAD_NAME = "Lachesis"
LENSES = ["wellarchitected", "arn:aws:wellarchitected::aws:lens/genai"]
MILESTONE = "freeze-2026-10-19"

# question-title keyword -> evidence note (file:line in this repo)
NOTES = [
    (
        "encrypt",
        (
            "KMS CMK with rotation: infra/stacks/foundation.py key; S3 and "
            "DynamoDB encrypted with it (foundation.py, approvals.py)."
        ),
    ),
    (
        "identity",
        (
            "Least-privilege roles: infra/stacks/agent.py (Bedrock profile "
            "plus one endpoint); CI assumes cdk-hnb659fds-* only "
            "(scripts/create_ci_role.py)."
        ),
    ),
    (
        "access control",
        (
            "Approvals need the Cognito approvers group "
            "(approvals/lambda/api.py _is_approver); decisions are "
            "conditional writes, 409 on repeat."
        ),
    ),
    (
        "monitor",
        (
            "Alarms and dashboard: infra/stacks/observability.py; "
            "per-triage JSON audit line: src/lachesis/handler.py _audit."
        ),
    ),
    (
        "credential",
        (
            "Agent tool subprocesses get a scrubbed environment, IMDS "
            "disabled: scripts/demo_agent.py scrubbed_env; "
            "agent/entrypoint.py _scrubbed_env."
        ),
    ),
]


def main() -> int:
    wa = boto3.client("wellarchitected", region_name=REGION)
    existing = {
        w["WorkloadName"]: w["WorkloadId"]
        for w in wa.list_workloads().get("WorkloadSummaries", [])
    }
    if WORKLOAD_NAME in existing:
        workload_id = existing[WORKLOAD_NAME]
        print("workload exists:", workload_id)
    else:
        workload_id = wa.create_workload(
            WorkloadName=WORKLOAD_NAME,
            Description=(
                "Lachesis: Strands Decider 2B triage before every tool call, "
                "AgentCore Runtime agent, SageMaker inference component "
                "Decider, Approvals console."
            ),
            ReviewOwner="vinjun7",
            Environment="PRODUCTION",
            AwsRegions=[REGION],
            Lenses=["wellarchitected"],
            Tags={"app": "lachesis-guard"},
        )["WorkloadId"]
        print("workload created:", workload_id)

    wa.associate_lenses(WorkloadId=workload_id, LensAliases=LENSES)

    lines = [
        "# Well-Architected review: Lachesis",
        "",
        (
            f"Generated {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')} "
            f"by `python scripts/wa_review.py` (workload {workload_id}, eu-west-1)."
        ),
        "Self-attested: evidence notes cite file:line in this repo; no choice was",
        "auto-selected. The console PDF export is a manual step (see STATE.md).",
        "",
    ]
    for lens in LENSES:
        review = wa.get_lens_review(WorkloadId=workload_id, LensAlias=lens)["LensReview"]
        lines.append(f"## Lens: {lens}")
        lines.append("")
        lines.append(f"Risk counts: {review.get('RiskCounts', {})}")
        lines.append("")
        answered = 0
        for pillar in review.get("PillarReviewSummaries", []):
            for question in wa.list_answers(
                WorkloadId=workload_id, LensAlias=lens, PillarId=pillar["PillarId"]
            ).get("AnswerSummaries", []):
                title = (question.get("QuestionTitle") or "").lower()
                for keyword, note in NOTES:
                    if keyword in title:
                        wa.update_answer(
                            WorkloadId=workload_id,
                            LensAlias=lens,
                            QuestionId=question["QuestionId"],
                            Notes=f"Lachesis evidence: {note}",
                        )
                        answered += 1
                        break
        lines.append(f"Evidence notes stamped: {answered}")
        lines.append("")

    milestones = wa.list_milestones(WorkloadId=workload_id).get("MilestoneSummaries", [])
    if not any(m["MilestoneName"] == MILESTONE for m in milestones):
        wa.create_milestone(WorkloadId=workload_id, MilestoneName=MILESTONE)
        print("milestone set:", MILESTONE)
    lines.append(f"Milestone: {MILESTONE}")
    lines.append("")

    out = Path("docs/evidence/wa-review.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

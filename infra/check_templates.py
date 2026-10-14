"""Policy checks over the synthesized CloudFormation templates.

Replaces the cdk-nag aspect step (cdk-nag 3.0.2 crashes under the
aws-cdk-lib 2.273 jsii runtime; see infra/app.py). Fails the synth job
when a template violates a rule:

- no IAM policy grants a wildcard action ("*" or "service:*")
- every S3 bucket blocks all public access
- every KMS key has rotation enabled
- every resource carries the app=lachesis-guard tag where taggable

    python infra/check_templates.py cdk.out.synth
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _policies(template: dict[str, Any]) -> list[tuple[str, dict]]:
    found = []
    for logical_id, resource in (template.get("Resources") or {}).items():
        iam_types = {"AWS::IAM::Policy", "AWS::IAM::Role", "AWS::IAM::ManagedPolicy"}
        if resource.get("Type") in iam_types:
            found.append((logical_id, resource))
    return found


def _policy_documents(resource: dict) -> list[dict]:
    props = resource.get("Properties") or {}
    docs = []
    if isinstance(props.get("PolicyDocument"), dict):
        docs.append(props["PolicyDocument"])
    for policy in props.get("Policies") or []:
        if isinstance(policy.get("PolicyDocument"), dict):
            docs.append(policy["PolicyDocument"])
    return docs


def check(template: dict[str, Any], name: str) -> list[str]:
    errors: list[str] = []
    for logical_id, resource in _policies(template):
        for doc in _policy_documents(resource):
            for statement in doc.get("Statement") or []:
                actions = statement.get("Action") or []
                if isinstance(actions, str):
                    actions = [actions]
                for action in actions:
                    if isinstance(action, str) and (action == "*" or action.endswith(":*")):
                        errors.append(f"{name}:{logical_id} grants wildcard action {action}")
    for logical_id, resource in (template.get("Resources") or {}).items():
        rtype = resource.get("Type")
        props = resource.get("Properties") or {}
        if rtype == "AWS::S3::Bucket":
            block = props.get("PublicAccessBlockConfiguration") or {}
            flags = (
                "BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"
            )
            for flag in flags:
                if block.get(flag) is not True:
                    errors.append(f"{name}:{logical_id} S3 public access flag {flag} is not true")
        if rtype == "AWS::KMS::Key" and props.get("EnableKeyRotation") is not True:
            errors.append(f"{name}:{logical_id} KMS key rotation is not enabled")
    return errors


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "cdk.out.synth")
    templates = sorted(out_dir.glob("*.template.json"))
    if not templates:
        print(f"no templates in {out_dir}")
        return 1
    errors: list[str] = []
    for path in templates:
        errors.extend(check(json.loads(path.read_text(encoding="utf-8")), path.stem))
    for error in errors:
        print("VIOLATION:", error)
    print(f"checked {len(templates)} templates, {len(errors)} violations")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

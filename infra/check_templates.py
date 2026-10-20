"""Policy checks over the synthesized CloudFormation templates.

Replaces the cdk-nag aspect step (cdk-nag 3.0.2 crashes under the
aws-cdk-lib 2.273 jsii runtime; see infra/app.py). Fails the synth job
when a template violates a rule:

- no IAM policy grants a wildcard action ("*" or "service:*")
- every S3 bucket blocks all public access
- every KMS key has rotation enabled
- LachesisAudit only (check_audit): the Firehose role's S3 statements
  name only the events/* and errors/* object ARNs and the bucket ARN,
  with no object read or delete; the dead-letter queue's only Allow
  principal is events.amazonaws.com, conditioned on aws:SourceArn of
  the audit rule; every target of that rule has InputPath $.detail

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


AUDIT_STACK = "LachesisAudit"
AUDIT_RULE_NAME = "audit-all"
AUDIT_DELIVERY_POLICY_PREFIX = "AuditDeliveryRoleDefaultPolicy"
AUDIT_OBJECT_SUFFIXES = ("/events/*", "/errors/*")
AUDIT_BUCKET_ACTIONS = frozenset(
    {"s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"}
)
AUDIT_OBJECT_ACTIONS = frozenset({"s3:PutObject", "s3:AbortMultipartUpload"})


def _listed(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _of_type(template: dict[str, Any], rtype: str) -> dict[str, dict]:
    return {
        logical_id: resource
        for logical_id, resource in (template.get("Resources") or {}).items()
        if resource.get("Type") == rtype
    }


def _is_bucket_arn(resource: Any, buckets: set[str]) -> bool:
    """{"Fn::GetAtt": [<bucket>, "Arn"]} for a bucket of this template."""
    if not isinstance(resource, dict) or set(resource) != {"Fn::GetAtt"}:
        return False
    target = resource["Fn::GetAtt"]
    return isinstance(target, list) and target[:1] != [] and target[0] in buckets and (
        target[1:] == ["Arn"]
    )


def _object_suffix(resource: Any, buckets: set[str]) -> str | None:
    """The key pattern of {"Fn::Join": ["", [<bucket arn>, "/<prefix>*"]]}."""
    if not isinstance(resource, dict) or set(resource) != {"Fn::Join"}:
        return None
    join = resource["Fn::Join"]
    if not (isinstance(join, list) and len(join) == 2 and join[0] == ""):
        return None
    parts = join[1]
    if not (isinstance(parts, list) and len(parts) == 2 and _is_bucket_arn(parts[0], buckets)):
        return None
    return parts[1] if isinstance(parts[1], str) else None


def check_audit(template: dict[str, Any], name: str) -> list[str]:
    """The LachesisAudit least-privilege and wiring rules (see the module
    docstring). Fails, too, when a resource the rules look at is missing,
    so a rename cannot make the check pass by finding nothing."""
    errors: list[str] = []
    buckets = set(_of_type(template, "AWS::S3::Bucket"))

    policies = {
        logical_id: resource
        for logical_id, resource in _of_type(template, "AWS::IAM::Policy").items()
        if logical_id.startswith(AUDIT_DELIVERY_POLICY_PREFIX)
    }
    if len(policies) != 1:
        errors.append(f"{name}: expected one Firehose delivery role policy, found {len(policies)}")
    s3_statements = 0
    for logical_id, resource in policies.items():
        for doc in _policy_documents(resource):
            for statement in doc.get("Statement") or []:
                actions = [a for a in _listed(statement.get("Action")) if isinstance(a, str)]
                s3_actions = [a for a in actions if a.startswith("s3:")]
                if not s3_actions:
                    continue
                s3_statements += 1
                if statement.get("Effect") != "Allow":
                    continue
                resources = _listed(statement.get("Resource"))
                if set(s3_actions) <= AUDIT_OBJECT_ACTIONS:
                    for res in resources:
                        if _object_suffix(res, buckets) not in AUDIT_OBJECT_SUFFIXES:
                            errors.append(
                                f"{name}:{logical_id} object write outside events/ and errors/"
                            )
                elif set(s3_actions) <= AUDIT_BUCKET_ACTIONS:
                    for res in resources:
                        if not _is_bucket_arn(res, buckets):
                            errors.append(f"{name}:{logical_id} bucket action on another resource")
                else:
                    extra = sorted(set(s3_actions) - AUDIT_OBJECT_ACTIONS - AUDIT_BUCKET_ACTIONS)
                    errors.append(f"{name}:{logical_id} S3 actions beyond delivery: {extra}")
    if policies and s3_statements == 0:
        errors.append(f"{name}: the Firehose delivery role policy has no S3 statement")

    rules = {
        logical_id: resource
        for logical_id, resource in _of_type(template, "AWS::Events::Rule").items()
        if (resource.get("Properties") or {}).get("Name") == AUDIT_RULE_NAME
    }
    if len(rules) != 1:
        errors.append(f"{name}: expected one rule named {AUDIT_RULE_NAME}, found {len(rules)}")
    for logical_id, resource in rules.items():
        targets = (resource.get("Properties") or {}).get("Targets") or []
        if not targets:
            errors.append(f"{name}:{logical_id} the audit rule has no target")
        for target in targets:
            if target.get("InputPath") != "$.detail":
                errors.append(f"{name}:{logical_id} a target does not send only $.detail")

    queue_policies = _of_type(template, "AWS::SQS::QueuePolicy")
    if not queue_policies:
        errors.append(f"{name}: no dead-letter queue policy")
    allowed_source = [{"Fn::GetAtt": [logical_id, "Arn"]} for logical_id in rules]
    for logical_id, resource in queue_policies.items():
        allows = [
            statement
            for doc in _policy_documents(resource)
            for statement in doc.get("Statement") or []
            if statement.get("Effect") == "Allow"
        ]
        if not allows:
            errors.append(f"{name}:{logical_id} the queue policy allows nothing")
        for statement in allows:
            if statement.get("Principal") != {"Service": "events.amazonaws.com"}:
                errors.append(f"{name}:{logical_id} a queue Allow is not for EventBridge only")
            source = ((statement.get("Condition") or {}).get("ArnEquals") or {}).get(
                "aws:SourceArn"
            )
            if source not in allowed_source:
                errors.append(f"{name}:{logical_id} a queue Allow is not bound to the audit rule")
    return errors


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "cdk.out.synth")
    templates = sorted(out_dir.glob("*.template.json"))
    if not templates:
        print(f"no templates in {out_dir}")
        return 1
    errors: list[str] = []
    for path in templates:
        template = json.loads(path.read_text(encoding="utf-8"))
        errors.extend(check(template, path.stem))
        # path.stem of LachesisAudit.template.json is LachesisAudit.template.
        if path.name.split(".", 1)[0] == AUDIT_STACK:
            errors.extend(check_audit(template, path.stem))
    for error in errors:
        print("VIOLATION:", error)
    print(f"checked {len(templates)} templates, {len(errors)} violations")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Unit tests for infra/check_templates.py (the cdk-nag replacement).

The checker runs in CI on the synthesized templates; these tests pin
its three rules on synthetic templates so a regression in the checker
itself fails the ordinary test job, not only a manual synth.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_templates", Path(__file__).resolve().parents[1] / "infra" / "check_templates.py"
)
assert _SPEC and _SPEC.loader
check_templates = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_templates)


def _template(resources):
    return {"Resources": resources}


def test_wildcard_action_is_a_violation():
    tpl = _template(
        {
            "P": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]
                    }
                },
            }
        }
    )
    errors = check_templates.check(tpl, "T")
    assert any("wildcard" in e for e in errors)


def test_scoped_actions_pass():
    tpl = _template(
        {
            "P": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "*"}
                        ]
                    }
                },
            }
        }
    )
    assert check_templates.check(tpl, "T") == []


def test_s3_public_access_and_kms_rotation_rules():
    tpl = _template(
        {
            "B": {"Type": "AWS::S3::Bucket", "Properties": {}},
            "K": {"Type": "AWS::KMS::Key", "Properties": {"EnableKeyRotation": False}},
        }
    )
    errors = check_templates.check(tpl, "T")
    assert any("S3 public access" in e for e in errors)
    assert any("KMS key rotation" in e for e in errors)

    ok = _template(
        {
            "B": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True,
                        "RestrictPublicBuckets": True,
                    }
                },
            },
            "K": {"Type": "AWS::KMS::Key", "Properties": {"EnableKeyRotation": True}},
        }
    )
    assert check_templates.check(ok, "T") == []


# --- LachesisAudit (check_audit) ------------------------------------------
#
# A template shaped the way CDK synthesizes infra/stacks/audit.py: the
# role's default policy with Fn::GetAtt and Fn::Join resources, the DLQ
# policy with the enforce_ssl Deny beside the rule's Allow, the rule
# with its InputPath.

_BUCKET_ARN = {"Fn::GetAtt": ["AuditBucketAB12", "Arn"]}
_RULE_ARN = {"Fn::GetAtt": ["AuditAllRule", "Arn"]}


def _objects(suffix):
    return {"Fn::Join": ["", [_BUCKET_ARN, suffix]]}


def _audit_template():
    return _template(
        {
            "AuditBucketAB12": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True,
                        "RestrictPublicBuckets": True,
                    }
                },
            },
            "AuditDeliveryRoleDefaultPolicyCD34": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["s3:AbortMultipartUpload", "s3:PutObject"],
                                "Resource": [_objects("/events/*"), _objects("/errors/*")],
                            },
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "s3:GetBucketLocation",
                                    "s3:ListBucket",
                                    "s3:ListBucketMultipartUploads",
                                ],
                                "Resource": _BUCKET_ARN,
                            },
                            {"Effect": "Allow", "Action": "logs:PutLogEvents", "Resource": "x"},
                        ]
                    }
                },
            },
            "AuditAllRule": {
                "Type": "AWS::Events::Rule",
                "Properties": {
                    "Name": "audit-all",
                    "Targets": [{"Id": "AuditFirehose", "InputPath": "$.detail"}],
                },
            },
            "AuditDlqPolicyEF56": {
                "Type": "AWS::SQS::QueuePolicy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Deny",
                                "Principal": {"AWS": "*"},
                                "Action": "sqs:*",
                                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                            },
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "events.amazonaws.com"},
                                "Action": "sqs:SendMessage",
                                "Condition": {"ArnEquals": {"aws:SourceArn": _RULE_ARN}},
                            },
                        ]
                    }
                },
            },
        }
    )


def _statements(tpl, logical_id):
    return tpl["Resources"][logical_id]["Properties"]["PolicyDocument"]["Statement"]


def test_the_audit_template_as_built_passes():
    assert check_templates.check_audit(_audit_template(), "LachesisAudit.template") == []


def test_an_audit_object_write_outside_the_two_prefixes_fails():
    tpl = _audit_template()
    _statements(tpl, "AuditDeliveryRoleDefaultPolicyCD34")[0]["Resource"].append(_objects("/*"))
    assert any("outside events/" in e for e in check_templates.check_audit(tpl, "A"))


def test_an_audit_object_read_or_delete_fails():
    for action in ("s3:GetObject", "s3:DeleteObject"):
        tpl = _audit_template()
        _statements(tpl, "AuditDeliveryRoleDefaultPolicyCD34")[0]["Action"].append(action)
        assert any("beyond delivery" in e for e in check_templates.check_audit(tpl, "A"))


def test_a_bucket_action_on_another_resource_fails():
    tpl = _audit_template()
    _statements(tpl, "AuditDeliveryRoleDefaultPolicyCD34")[1]["Resource"] = "*"
    assert any("bucket action" in e for e in check_templates.check_audit(tpl, "A"))


def test_a_dlq_allow_for_anyone_else_fails():
    tpl = _audit_template()
    _statements(tpl, "AuditDlqPolicyEF56")[1]["Principal"] = {"AWS": "*"}
    assert any("EventBridge only" in e for e in check_templates.check_audit(tpl, "A"))
    tpl = _audit_template()
    del _statements(tpl, "AuditDlqPolicyEF56")[1]["Condition"]
    assert any("audit rule" in e for e in check_templates.check_audit(tpl, "A"))


def test_a_target_without_the_detail_input_path_fails():
    tpl = _audit_template()
    del tpl["Resources"]["AuditAllRule"]["Properties"]["Targets"][0]["InputPath"]
    assert any("$.detail" in e for e in check_templates.check_audit(tpl, "A"))


def test_the_audit_check_is_never_vacuous():
    errors = check_templates.check_audit(_template({}), "A")
    assert any("delivery role policy" in e for e in errors)
    assert any("audit-all" in e for e in errors)
    assert any("queue policy" in e for e in errors)


def test_main_runs_the_audit_check_only_on_the_audit_template(tmp_path, monkeypatch, capsys):
    import json

    (tmp_path / "LachesisAudit.template.json").write_text(json.dumps(_template({})))
    (tmp_path / "LachesisApprovals.template.json").write_text(json.dumps(_template({})))
    monkeypatch.setattr("sys.argv", ["check_templates.py", str(tmp_path)])
    assert check_templates.main() == 1
    out = capsys.readouterr().out
    assert "LachesisAudit.template:" in out and "LachesisApprovals" not in out
    (tmp_path / "LachesisAudit.template.json").write_text(json.dumps(_audit_template()))
    assert check_templates.main() == 0

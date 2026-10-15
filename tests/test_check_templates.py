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

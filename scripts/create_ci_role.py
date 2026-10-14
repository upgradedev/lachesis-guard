"""Create LachesisCiRole once, locally, with the owner's credentials.

ADDENDUM day-one blocker 1: no OIDC role trusts the repo. This script is
the one reviewed local deploy of that role. It imports the shared GitHub
OIDC provider by ARN (it never creates or modifies the provider), trusts
only the two GitHub environments aws-deploy and aws-destroy of this
repo, and grants exactly one thing: assuming the CDK bootstrap roles
(cdk-hnb659fds-*). Deploys then run from GitHub Actions through this
role. Re-running is safe: an existing role gets its trust updated.

    python scripts/create_ci_role.py
"""

from __future__ import annotations

import json
import subprocess

import boto3

REPO = "upgradedev/lachesis-guard"
ENVIRONMENTS = ("aws-deploy", "aws-destroy")


def _gh_api(path: str) -> dict:
    out = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out)


def main() -> int:
    iam = boto3.client("iam")
    account = boto3.client("sts").get_caller_identity()["Account"]
    provider = f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com"

    repo_id = _gh_api(f"repos/{REPO}")["id"]
    owner_id = _gh_api("users/upgradedev")["id"]

    subjects: list[str] = []
    for env in ENVIRONMENTS:
        subjects.append(f"repo:{REPO}:environment:{env}")
        subjects.append(
            f"repo:upgradedev@{owner_id}/lachesis-guard@{repo_id}:environment:{env}"
        )

    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Federated": provider},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                    },
                    "StringLike": {"token.actions.githubusercontent.com:sub": subjects},
                },
            }
        ],
    }
    try:
        role = iam.create_role(
            RoleName="LachesisCiRole",
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="GitHub Actions OIDC role for lachesis-guard deploys",
            Tags=[{"Key": "app", "Value": "lachesis-guard"}],
        )["Role"]
        print("created", role["Arn"])
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(
            RoleName="LachesisCiRole", PolicyDocument=json.dumps(trust)
        )
        print("exists, trust updated")

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AssumeCdkRoles",
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Resource": f"arn:aws:iam::{account}:role/cdk-hnb659fds-*",
            },
            {
                "Sid": "ReadSelf",
                "Effect": "Allow",
                "Action": ["iam:GetRole", "sts:GetCallerIdentity"],
                "Resource": "*",
            },
        ],
    }
    iam.put_role_policy(
        RoleName="LachesisCiRole",
        PolicyName="LachesisCiAssumeCdk",
        PolicyDocument=json.dumps(policy),
    )
    print("policy attached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

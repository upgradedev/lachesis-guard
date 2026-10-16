"""Create the Lachesis approver without ever touching their password (F6).

Runs admin-create-user with --message-action SUPPRESS (no invitation
email, no temporary password sent anywhere) and adds the user to the
"approvers" group, without which every approvals call returns 403.
The owner then sets a permanent password in their own PowerShell with
admin-set-user-password --permanent; this script never accepts,
generates or prints a password. Do not use the Cognito console's
"Create user": that path leaves the user in FORCE_CHANGE_PASSWORD and
the approvals console has no NEW_PASSWORD_REQUIRED handler.

    python scripts/create_approver.py --username <name>

The user pool id is read from the LachesisApprovals stack outputs
unless --user-pool-id is given. Do not enrol TOTP: the approvals
console has no MFA enrolment surface.
"""

from __future__ import annotations

import argparse

import boto3

REGION = "eu-west-1"
STACK = "LachesisApprovals"
GROUP = "approvers"


def pool_id_from_stack(cfn) -> str:
    outputs = cfn.describe_stacks(StackName=STACK)["Stacks"][0].get("Outputs", [])
    for out in outputs:
        if out["OutputKey"] == "UserPoolId":
            return out["OutputValue"]
    raise SystemExit(f"UserPoolId output not found on stack {STACK}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--user-pool-id", default="")
    args = parser.parse_args()

    session = boto3.session.Session(region_name=REGION)
    pool_id = args.user_pool_id or pool_id_from_stack(session.client("cloudformation"))
    idp = session.client("cognito-idp")
    idp.admin_create_user(
        UserPoolId=pool_id,
        Username=args.username,
        MessageAction="SUPPRESS",
    )
    idp.admin_add_user_to_group(
        UserPoolId=pool_id, Username=args.username, GroupName=GROUP
    )
    print(f"created {args.username} in pool {pool_id}, group {GROUP}")
    print("owner step (run it yourself, never paste the password here):")
    print(
        "  aws cognito-idp admin-set-user-password --region eu-west-1 "
        f"--user-pool-id {pool_id} --username {args.username} "
        "--password <your-password> --permanent"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import boto3, json
iam = boto3.client("iam")
acct = boto3.client("sts").get_caller_identity()["Account"]
provider = f"arn:aws:iam::{acct}:oidc-provider/token.actions.githubusercontent.com"

import subprocess
def gh(*args):
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True)
repo = json.loads(gh("repos/upgradedev/lachesis-guard").stdout)
org = json.loads(gh("users/upgradedev").stdout)
repo_id, owner_id = repo["id"], org["id"]
print("repo_id", repo_id, "owner_id", owner_id)

subs = []
for env in ("aws-deploy", "aws-destroy"):
    subs.append(f"repo:upgradedev/lachesis-guard:environment:{env}")
    subs.append(f"repo:upgradedev@{owner_id}/lachesis-guard@{repo_id}:environment:{env}")

trust = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Federated": provider},
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {
            "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
            "StringLike": {"token.actions.githubusercontent.com:sub": subs},
        },
    }],
}
tags = [{"Key": "app", "Value": "lachesis-guard"}]
try:
    role = iam.create_role(RoleName="LachesisCiRole", AssumeRolePolicyDocument=json.dumps(trust),
                           Description="GitHub Actions OIDC role for lachesis-guard deploys",
                           Tags=tags)["Role"]
    print("created", role["Arn"])
except iam.exceptions.EntityAlreadyExistsException:
    iam.update_assume_role_policy(RoleName="LachesisCiRole", PolicyDocument=json.dumps(trust))
    print("exists, trust updated")

policy = {
    "Version": "2012-10-17",
    "Statement": [
        {"Sid": "AssumeCdkRoles", "Effect": "Allow", "Action": "sts:AssumeRole",
         "Resource": f"arn:aws:iam::{acct}:role/cdk-hnb659fds-*"},
        {"Sid": "ReadSelf", "Effect": "Allow", "Action": ["iam:GetRole", "sts:GetCallerIdentity"],
         "Resource": "*"},
    ],
}
iam.put_role_policy(RoleName="LachesisCiRole", PolicyName="LachesisCiAssumeCdk",
                    PolicyDocument=json.dumps(policy))
print("policy attached")

for env in ("aws-deploy", "aws-destroy"):
    r = gh("-X", "PUT", f"repos/upgradedev/lachesis-guard/environments/{env}", "-f", "name=dummy")
    print(env, "->", r.returncode)

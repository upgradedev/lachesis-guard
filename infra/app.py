"""Lachesis infrastructure (CDK Python, eu-west-1).

Stacks, deployed in this order through .github/workflows/iac.yml:
  LachesisFoundation   KMS CMK, sessions bucket, ECR repos, image builds, budget
  LachesisDecider      Decider 2B on a SageMaker inference component (BYOC)
  LachesisAgent        the Strands agent on AgentCore Runtime V2
  LachesisApprovals    approval API, console, runner, sweeper
  LachesisObservability alarms and log retention for the above

Everything is tagged app=lachesis-guard. Deploys run only from GitHub
Actions through LachesisCiRole (OIDC); the role itself is created once
by scripts/create_ci_role.py, per the ADDENDUM day-one blocker list.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Aspects, Tags
from cdk_nag import AwsSolutionsChecks
from stacks.foundation import FoundationStack

try:
    from stacks.agent import AgentStack
    from stacks.approvals import ApprovalsStack
    from stacks.decider import DeciderStack
    from stacks.observability import ObservabilityStack
    HAVE_ALL_STACKS = True
except ImportError:  # stacks land one at a time during the build
    HAVE_ALL_STACKS = False


app = cdk.App()
env = cdk.Environment(account=app.node.try_get_context("account"), region="eu-west-1")

foundation = FoundationStack(app, "LachesisFoundation", env=env)
if HAVE_ALL_STACKS:
    decider = DeciderStack(app, "LachesisDecider", env=env, foundation=foundation)
    agent = AgentStack(app, "LachesisAgent", env=env, foundation=foundation, decider=decider)
    ApprovalsStack(app, "LachesisApprovals", env=env, foundation=foundation, agent=agent)
    ObservabilityStack(
        app, "LachesisObservability", env=env, foundation=foundation, decider=decider, agent=agent
    )

for stack in app.node.find_all():
    if isinstance(stack, cdk.Stack):
        Tags.of(stack).add("app", "lachesis-guard")

Aspects.of(app).add(AwsSolutionsChecks())

app.synth()

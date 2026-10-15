"""LachesisAgent: the Strands agent on AgentCore Runtime V2 (A6).

One runtime, PUBLIC network mode, arm64 image from the foundation ECR
repo lachesis-agent. The execution role is least-privilege per the
ADDENDUM: Bedrock on the EU Sonnet profile and its foundation models,
sagemaker:InvokeEndpoint on the one Decider endpoint, S3 on the session
prefix only, logs, and the foundation CMK. The Lachesis handler's
verdict cache lives in agent.state, which the S3SessionManager persists,
so a pause survives the microVM idling out.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from constructs import Construct

MODEL_PROFILE = "eu.anthropic.claude-sonnet-5-5"


class AgentStack(cdk.Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, foundation, decider, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        role = iam.Role(
            self,
            "AgentExecutionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="AgentCore execution role for the Lachesis agent",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:eu-west-1:{self.account}:inference-profile/{MODEL_PROFILE}",
                    "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sagemaker:InvokeEndpoint"],
                resources=[
                    f"arn:aws:sagemaker:eu-west-1:{self.account}:endpoint/lachesis-decider",
                    f"arn:aws:sagemaker:eu-west-1:{self.account}:inference-component/lachesis-decider-2b",
                ],
            )
        )
        foundation.sessions_bucket.grant_read_write(role)
        foundation.key.grant_decrypt(role)
        foundation.key.grant_encrypt(role)
        foundation.agent_repo.grant_pull(role)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=["*"],
            )
        )

        # AgentCore validates the ECR pull when the runtime is created,
        # so the role's pull policy must exist first.
        _default_policy = role.node.try_find_child("DefaultPolicy")

        runtime = agentcore.CfnRuntime(
            self,
            "LachesisRuntime",
            agent_runtime_name="lachesis_agent",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=foundation.agent_repo.repository_uri_for_tag("latest"),
                ),
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC",
            ),
            protocol_configuration="HTTP",
            role_arn=role.role_arn,
            environment_variables={
                "SESSIONS_BUCKET": foundation.sessions_bucket.bucket_name,
                "MODEL_ID": MODEL_PROFILE,
                "DECIDER_ENDPOINT": decider.endpoint_name,
                "DECIDER_COMPONENT": decider.inference_component_name,
                "AGENT_CODE_REV": "2",
            },
            lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                idle_runtime_session_timeout=900,
                max_lifetime=28800,
            ),
            tags={"app": "lachesis-guard"},
        )

        if _default_policy is not None:
            runtime.node.add_dependency(_default_policy)

        self.runtime = runtime
        self.runtime_id = runtime.attr_agent_runtime_id
        self.runtime_arn = runtime.attr_agent_runtime_arn

        cdk.CfnOutput(self, "RuntimeId", value=self.runtime_id)
        cdk.CfnOutput(self, "RuntimeArn", value=self.runtime_arn)

"""LachesisDecider: Decider 2B on a SageMaker inference component.

BYOC image (foundation ECR repo lachesis-decider) behind one inference
component on ml.g5.xlarge. Two independent scaling layers, and only the
second one was true before 2026-10-15: Application Auto Scaling moves
the component's DesiredCopyCount between 0 and 1, but the endpoint
INSTANCE bills around the clock unless the endpoint config's production
variant carries ManagedInstanceScaling (MinInstanceCount 0), which this
stack sets through an L1 property override together with the pinned
InferenceAmiVersion. With no instance there is nothing to bill: $0 idle,
about $1.57/h while warm (ml.g5.xlarge, eu-west-1). A step-scaling
policy on NoCapacityInvocationFailures (in LachesisDeciderIc) is what
wakes an instance for a cold invoke. The container contract is
decider-image/adapter.py: /ping gates on the real server health,
/invocations forwards to /v1/systemone, one forward pass at a time (a
busy pass answers 503 and callers fail closed).

Note on the architect brief: it listed ManagedInstanceScaling and
InferenceAmiVersion as CFN properties of the inference component; the
CDK 2.273.0 CfnInferenceComponent surface has neither (verified against
the published API reference), so scaling is Application Auto Scaling on
DesiredCopyCount and the AMI is the endpoint default. Same behaviour,
real property names.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sagemaker as sagemaker
from constructs import Construct


class DeciderStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, foundation, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        role = iam.Role(
            self,
            "DeciderExecutionRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            description="SageMaker execution role for the Lachesis Decider",
        )
        foundation.decider_repo.grant_pull(role)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:PutMetricData",
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=["*"],
            )
        )

        image_uri = foundation.decider_repo.repository_uri_for_tag("latest")
        # SageMaker validates the pull when the model is created, so the
        # role's pull policy must exist first.
        _default_policy = role.node.try_find_child("DefaultPolicy")

        model = sagemaker.CfnModel(
            self,
            "DeciderModel",
            execution_role_arn=role.role_arn,
            primary_container=sagemaker.CfnModel.ContainerDefinitionProperty(
                image=image_uri,
                mode="SingleModel",
            ),
            tags=[cdk.CfnTag(key="app", value="lachesis-guard")],
        )
        if _default_policy is not None:
            model.node.add_dependency(_default_policy)

        endpoint_config = sagemaker.CfnEndpointConfig(
            self,
            "DeciderEndpointConfig",
            # Required for inference-component endpoints: the config-level
            # role is what pulls the component's image.
            execution_role_arn=role.role_arn,
            production_variants=[
                # No model_name: an endpoint whose variant names a model
                # cannot host inference components ("Inference Components
                # are not supported in this Endpoint", first deploy). The
                # component below carries the container instead.
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="AllTraffic",
                    initial_instance_count=1,
                    instance_type="ml.g5.xlarge",
                )
                # ManagedInstanceScaling and InferenceAmiVersion are set
                # through the overrides below: they are what scale the
                # endpoint INSTANCE (not just the model copy) to zero.
            ],
            tags=[cdk.CfnTag(key="app", value="lachesis-guard")],
        )

        endpoint_config.add_property_override(
            "ProductionVariants.0.ManagedInstanceScaling",
            {
                "Status": "ENABLED",
                "MinInstanceCount": 0,
                "MaxInstanceCount": 1,
            },
        )
        endpoint_config.add_property_override(
            "ProductionVariants.0.InferenceAmiVersion",
            "al2023-ami-sagemaker-inference-gpu-4-1",
        )

        endpoint = sagemaker.CfnEndpoint(
            self,
            "DeciderEndpoint",
            endpoint_name="lachesis-decider",
            endpoint_config_name=endpoint_config.attr_endpoint_config_name,
            tags=[cdk.CfnTag(key="app", value="lachesis-guard")],
        )

        # The inference component lives in LachesisDeciderIc, deployed
        # after this stack: a fresh endpoint refuses component creation
        # with "not enough memory available" for its first minutes
        # (proven by hand, 2026-10-14), and CloudFormation has no retry
        # for that. scripts/apply_decider_ic.py is the emergency manual
        # path with the same specification.
        self.inference_component = None

        self.endpoint_name = endpoint.attr_endpoint_name
        self.inference_component_name = "lachesis-decider-2b"

        cdk.CfnOutput(self, "EndpointName", value=self.endpoint_name)
        cdk.CfnOutput(self, "InferenceComponentName", value=self.inference_component_name)

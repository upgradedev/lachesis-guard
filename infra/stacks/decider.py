"""LachesisDecider: Decider 2B on a SageMaker inference component.

BYOC image (foundation ECR repo lachesis-decider) behind one inference
component on ml.g5.xlarge. Application Auto Scaling moves DesiredCopyCount
between 0 and 1, so the GPU costs nothing while idle (judging lane (c):
scale to zero in Gate 1, warm in the Gate 2 week). The container contract
is decider-image/adapter.py: /ping gates on the real server health,
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
from aws_cdk import aws_applicationautoscaling as appscaling
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
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="AllTraffic",
                    model_name=model.attr_model_name,
                    initial_instance_count=1,
                    instance_type="ml.g5.xlarge",
                )
            ],
            tags=[cdk.CfnTag(key="app", value="lachesis-guard")],
        )

        endpoint = sagemaker.CfnEndpoint(
            self,
            "DeciderEndpoint",
            endpoint_name="lachesis-decider",
            endpoint_config_name=endpoint_config.attr_endpoint_config_name,
            tags=[cdk.CfnTag(key="app", value="lachesis-guard")],
        )

        component = sagemaker.CfnInferenceComponent(
            self,
            "DeciderInferenceComponent",
            endpoint_name=endpoint.attr_endpoint_name,
            inference_component_name="lachesis-decider-2b",
            variant_name="AllTraffic",
            runtime_config=sagemaker.CfnInferenceComponent.InferenceComponentRuntimeConfigProperty(
                copy_count=1,
            ),
            specification=sagemaker.CfnInferenceComponent.InferenceComponentSpecificationProperty(
                model_name=model.attr_model_name,
                container=sagemaker.CfnInferenceComponent.InferenceComponentContainerSpecificationProperty(
                    image=image_uri,
                ),
                compute_resource_requirements=(
                    sagemaker.CfnInferenceComponent.InferenceComponentComputeResourceRequirementsProperty(
                        number_of_accelerator_devices_required=1,
                        number_of_cpu_cores_required=4,
                        min_memory_required_in_mb=12288,
                        max_memory_required_in_mb=20480,
                    )
                ),
                startup_parameters=sagemaker.CfnInferenceComponent.InferenceComponentStartupParametersProperty(
                    container_startup_health_check_timeout_in_seconds=900,
                    model_data_download_timeout_in_seconds=1800,
                ),
            ),
            tags=[cdk.CfnTag(key="app", value="lachesis-guard")],
        )
        self.inference_component = component

        scaling_target = appscaling.CfnScalableTarget(
            self,
            "DeciderScalingTarget",
            service_namespace="sagemaker",
            resource_id="inference-component/lachesis-decider-2b",
            scalable_dimension="sagemaker:inference-component:DesiredCopyCount",
            min_capacity=0,
            max_capacity=1,
            suspended_state=appscaling.CfnScalableTarget.SuspendedStateProperty(
                dynamic_scaling_in_suspended=False,
                dynamic_scaling_out_suspended=False,
                scheduled_scaling_suspended=False,
            ),
        )
        scaling_target.add_dependency(component)

        policy = appscaling.CfnScalingPolicy(
            self,
            "DeciderScalingPolicy",
            policy_name="lachesis-decider-invocations",
            policy_type="TargetTrackingScaling",
            service_namespace="sagemaker",
            resource_id="inference-component/lachesis-decider-2b",
            scalable_dimension="sagemaker:inference-component:DesiredCopyCount",
            target_tracking_scaling_policy_configuration=(
                appscaling.CfnScalingPolicy.TargetTrackingScalingPolicyConfigurationProperty(
                    predefined_metric_specification=(
                        appscaling.CfnScalingPolicy.PredefinedMetricSpecificationProperty(
                            predefined_metric_type="SageMakerInferenceComponentInvocationsPerCopy",
                        )
                    ),
                    target_value=5.0,
                    scale_in_cooldown=600,
                    scale_out_cooldown=60,
                )
            ),
        )

        policy.add_dependency(scaling_target)

        self.endpoint_name = endpoint.attr_endpoint_name
        self.inference_component_name = component.inference_component_name

        cdk.CfnOutput(self, "EndpointName", value=self.endpoint_name)
        cdk.CfnOutput(self, "InferenceComponentName", value=self.inference_component_name)

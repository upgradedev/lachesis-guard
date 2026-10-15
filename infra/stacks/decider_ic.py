"""LachesisDeciderIc: the inference component and its scale 0..1 policy.

A separate stack from LachesisDecider on purpose: a fresh endpoint
refuses component creation with "There is not enough memory available
for this Inference Component" for its first minutes, even with a
specification the warmed endpoint later accepts (proven by hand with
boto3 on 2026-10-14, identical request parameters both times; see
TRAPS.md). CloudFormation has no retry for that window, so the runbook
is: deploy LachesisDecider, wait, then deploy this stack. The endpoint
is referenced by name, never by cross-stack attribute, so the two
stacks stay decoupled.

Specification, proven InService by hand before it was written here:
2 CPU cores, 1 accelerator, 2048-6144 MB (the scheduler's allocatable
envelope on ml.g5.xlarge is far below the raw instance size).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_applicationautoscaling as appscaling
from aws_cdk import aws_sagemaker as sagemaker
from constructs import Construct

ENDPOINT_NAME = "lachesis-decider"
COMPONENT_NAME = "lachesis-decider-2b"


class DeciderIcStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, foundation, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        image_uri = foundation.decider_repo.repository_uri_for_tag("latest")

        component = sagemaker.CfnInferenceComponent(
            self,
            "DeciderInferenceComponent",
            endpoint_name=ENDPOINT_NAME,
            inference_component_name=COMPONENT_NAME,
            variant_name="AllTraffic",
            runtime_config=sagemaker.CfnInferenceComponent.InferenceComponentRuntimeConfigProperty(
                copy_count=1,
            ),
            specification=sagemaker.CfnInferenceComponent.InferenceComponentSpecificationProperty(
                container=sagemaker.CfnInferenceComponent.InferenceComponentContainerSpecificationProperty(
                    image=image_uri,
                ),
                compute_resource_requirements=(
                    sagemaker.CfnInferenceComponent.InferenceComponentComputeResourceRequirementsProperty(
                        number_of_accelerator_devices_required=1,
                        number_of_cpu_cores_required=2,
                        min_memory_required_in_mb=2048,
                        max_memory_required_in_mb=6144,
                    )
                ),
                startup_parameters=sagemaker.CfnInferenceComponent.InferenceComponentStartupParametersProperty(
                    container_startup_health_check_timeout_in_seconds=900,
                    model_data_download_timeout_in_seconds=1800,
                ),
            ),
            tags=[cdk.CfnTag(key="app", value="lachesis-guard")],
        )

        scaling_target = appscaling.CfnScalableTarget(
            self,
            "DeciderScalingTarget",
            service_namespace="sagemaker",
            resource_id=f"inference-component/{COMPONENT_NAME}",
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
            resource_id=f"inference-component/{COMPONENT_NAME}",
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

        cdk.CfnOutput(self, "InferenceComponentName", value=COMPONENT_NAME)

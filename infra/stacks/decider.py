"""LachesisDecider: Decider 2B on a SageMaker inference component.

BYOC image (foundation ECR repo lachesis-decider), one ml.g5.xlarge,
ManagedInstanceScaling 0..1 copies so the GPU costs nothing while idle
(judging lane (c): scale to zero in Gate 1, warm in the Gate 2 week).
The container contract is decider-image/adapter.py: /ping gates on the
real server health, /invocations forwards to /v1/systemone, one forward
pass at a time (a busy pass answers 503 and callers fail closed).
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

        endpoint_config = sagemaker.CfnEndpointConfig(
            self,
            "DeciderEndpointConfig",
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="AllTraffic",
                    model_name=model.attr_model_name,
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

        self.inference_component = sagemaker.CfnInferenceComponent(
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
                compute_resource_requirements=sagemaker.CfnInferenceComponent.ComputeResourceRequirementsProperty(
                    number_of_accelerator_devices_required=1,
                    number_of_cpu_cores_required=4,
                    min_memory_required_in_mb=12288,
                    max_memory_required_in_mb=20480,
                ),
                startup_parameters=sagemaker.CfnInferenceComponent.StartupParametersProperty(
                    container_startup_health_check_timeout_in_seconds=900,
                    model_data_download_timeout_in_seconds=1800,
                ),
                instance_type="ml.g5.xlarge",
                inference_ami_version="al2023-ami-sagemaker-inference-gpu-4-1",
            ),
            managed_instance_scaling=sagemaker.CfnInferenceComponent.ManagedInstanceScalingProperty(
                status="ENABLED",
                min_instance_count=0,
                max_instance_count=1,
            ),
            tags=[cdk.CfnTag(key="app", value="lachesis-guard")],
        )

        self.endpoint_name = endpoint.attr_endpoint_name
        self.inference_component_name = self.inference_component.inference_component_name

        cdk.CfnOutput(self, "EndpointName", value=self.endpoint_name)
        cdk.CfnOutput(self, "InferenceComponentName", value=self.inference_component_name)

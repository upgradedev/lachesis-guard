"""Apply the Decider inference component (and its scaling) after deploy.

Why this is a script and not CloudFormation: a fresh SageMaker endpoint
refuses inference-component creation with "There is not enough memory
available for this Inference Component" for its first minutes, even with
a specification the warmed endpoint later accepts (proven by hand with
boto3 on 2026-10-14, identical request parameters both times; see
TRAPS.md). CloudFormation has no retry for that window, so this script
mirrors the stack's specification and retries with backoff until the
endpoint settles, then registers the scale 0..1 target and policy.
Idempotent: an existing healthy component is left alone.

    python scripts/apply_decider_ic.py
"""

from __future__ import annotations

import time

import boto3
from botocore.exceptions import ClientError

REGION = "eu-west-1"
ENDPOINT = "lachesis-decider"
COMPONENT = "lachesis-decider-2b"
VARIANT = "AllTraffic"
RESOURCE_ID = f"inference-component/{COMPONENT}"
DIMENSION = "sagemaker:inference-component:DesiredCopyCount"
MAX_ATTEMPTS = 14
RETRY_SECONDS = 180

SPECIFICATION = {
    "ComputeResourceRequirements": {
        "NumberOfCpuCoresRequired": 2,
        "NumberOfAcceleratorDevicesRequired": 1,
        "MinMemoryRequiredInMb": 2048,
        "MaxMemoryRequiredInMb": 6144,
    },
    "StartupParameters": {
        "ContainerStartupHealthCheckTimeoutInSeconds": 900,
        "ModelDataDownloadTimeoutInSeconds": 1800,
    },
}


def _image_uri(sm) -> str:
    account = boto3.client("sts").get_caller_identity()["Account"]
    return f"{account}.dkr.ecr.{REGION}.amazonaws.com/lachesis-decider:latest"


def _component_status(sm) -> str | None:
    try:
        return sm.describe_inference_component(  # type: ignore[no-any-return]
            InferenceComponentName=COMPONENT
        )["InferenceComponentStatus"]
    except ClientError:
        return None


def _wait_gone(sm) -> None:
    for _ in range(30):
        if _component_status(sm) is None:
            return
        time.sleep(20)


def ensure_component(sm) -> str:
    status = _component_status(sm)
    if status == "InService":
        return status
    specification = dict(SPECIFICATION)
    specification["Container"] = {"Image": _image_uri(sm)}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if _component_status(sm) is not None:
            sm.delete_inference_component(InferenceComponentName=COMPONENT)
            _wait_gone(sm)
        sm.create_inference_component(
            InferenceComponentName=COMPONENT,
            EndpointName=ENDPOINT,
            VariantName=VARIANT,
            Specification=specification,
            RuntimeConfig={"CopyCount": 1},
            Tags=[{"Key": "app", "Value": "lachesis-guard"}],
        )
        print(f"attempt {attempt}: created, waiting for InService")
        for _ in range(80):
            time.sleep(20)
            status = _component_status(sm)
            if status == "InService":
                return status
            if status == "Failed":
                break
        print(f"attempt {attempt}: not InService (status={status}); backing off")
        time.sleep(RETRY_SECONDS)
    raise SystemExit(f"inference component never reached InService after {MAX_ATTEMPTS} attempts")


def ensure_scaling() -> None:
    aas = boto3.client("application-autoscaling", region_name=REGION)
    aas.register_scalable_target(
        ServiceNamespace="sagemaker",
        ResourceId=RESOURCE_ID,
        ScalableDimension=DIMENSION,
        MinCapacity=0,
        MaxCapacity=1,
    )
    aas.put_scaling_policy(
        PolicyName="lachesis-decider-invocations",
        ServiceNamespace="sagemaker",
        ResourceId=RESOURCE_ID,
        ScalableDimension=DIMENSION,
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration={
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "SageMakerInferenceComponentInvocationsPerCopy"
            },
            "TargetValue": 5.0,
            "ScaleInCooldown": 600,
            "ScaleOutCooldown": 60,
        },
    )
    print("scaling target 0..1 and target-tracking policy registered")


def main() -> int:
    sm = boto3.client("sagemaker", region_name=REGION)
    print("component:", ensure_component(sm))
    ensure_scaling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

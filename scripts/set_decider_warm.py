"""Warm or cool the Decider GPU on purpose (judging lane (c)).

Measured on 2026-10-15: at zero copies an InvokeEndpoint fails at once
("Inference Component has no capacity to process this request") and the
target-tracking policy does not scale out from a single cold call
(DesiredCopyCount still 0 after 6+ minutes). So the operating rule is
explicit: warm before recording and for the Gate 2 week, cool when idle.
Warming flips the scalable target's MinCapacity to 1; the existing
target-tracking policy then holds one copy. Cooling returns MinCapacity
to 0 and idle scale-in follows.

    python scripts/set_decider_warm.py --warm
    python scripts/set_decider_warm.py --cold
"""

from __future__ import annotations

import argparse
import time

import boto3

REGION = "eu-west-1"
RESOURCE_ID = "inference-component/lachesis-decider-2b"
DIMENSION = "sagemaker:inference-component:DesiredCopyCount"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--warm", action="store_true", help="hold one GPU copy")
    group.add_argument("--cold", action="store_true", help="allow scale to zero")
    args = parser.parse_args()

    aas = boto3.client("application-autoscaling", region_name=REGION)
    aas.register_scalable_target(
        ServiceNamespace="sagemaker",
        ResourceId=RESOURCE_ID,
        ScalableDimension=DIMENSION,
        MinCapacity=1 if args.warm else 0,
        MaxCapacity=1,
    )
    print(f"scalable target MinCapacity -> {1 if args.warm else 0}")

    if args.warm:
        sm = boto3.client("sagemaker", region_name=REGION)
        deadline = time.time() + 1500
        while time.time() < deadline:
            rc = sm.describe_inference_component(
                InferenceComponentName="lachesis-decider-2b"
            )["RuntimeConfig"]
            print(f"desired={rc.get('DesiredCopyCount')} current={rc.get('CurrentCopyCount')}")
            if rc.get("CurrentCopyCount", 0) >= 1:
                print("decider warm")
                return 0
            time.sleep(20)
        raise SystemExit("decider did not warm in 25 minutes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

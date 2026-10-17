"""Warm or cool the Decider GPU on purpose (judging lane (c)).

Measured behaviour, 2026-10-15, all times UTC (CloudTrail and the
alarm histories; see docs/evidence/GPU_2026-10-13.md):

- At zero copies an InvokeEndpoint fails at once ("Inference Component
  has no capacity to process this request"); nothing waits for you.
- Warming is explicit: --warm sets the scalable target's MinCapacity
  to 1 and the step-policy alarm scales a copy out on the first
  no-capacity failure. The target-tracking policy then holds one copy.
- A cold placement can stall: on 2026-10-15 the copy requests at
  20:37:22 and 20:40:22 sat for about 25 minutes with the instance
  already up, and a manual copy-count reset (0, then re-issued) placed
  the copy in 98 seconds. This script performs that one reset itself
  when copies are still 0 ten minutes after the instance is up.
- Warm placements after that took 2m44s and 2m48s; a full cold wake
  from 0 instances took about 30 minutes wall clock including the
  stall. The wait budget here is 45 minutes, and every transition is
  printed with its timestamp so a drill can be committed as evidence.
- Cooling returns MinCapacity to 0; copies scale in first, then the
  endpoint instance (about 14 minutes later on 2026-10-15).

    python scripts/set_decider_warm.py --warm
    python scripts/set_decider_warm.py --cold
"""

from __future__ import annotations

import argparse
import datetime
import time

import boto3

REGION = "eu-west-1"
RESOURCE_ID = "inference-component/lachesis-decider-2b"
DIMENSION = "sagemaker:inference-component:DesiredCopyCount"
COMPONENT = "lachesis-decider-2b"
ENDPOINT = "lachesis-decider"
DEADLINE_SECONDS = 2700  # 45 minutes
STALL_AFTER_SECONDS = 600  # instance up, no copy for 10 min -> reset once
POLL_SECONDS = 20


def _stamp(now: float) -> str:
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(
        timespec="seconds"
    )


def _counts(sm) -> tuple[int, int, int]:
    ep = sm.describe_endpoint(EndpointName=ENDPOINT)
    instances = sum(
        v.get("CurrentInstanceCount", 0) for v in ep.get("ProductionVariants", [])
    )
    rc = sm.describe_inference_component(InferenceComponentName=COMPONENT)[
        "RuntimeConfig"
    ]
    return instances, int(rc.get("DesiredCopyCount", 0)), int(rc.get("CurrentCopyCount", 0))


def warm(sm, aas, *, deadline=DEADLINE_SECONDS, stall_after=STALL_AFTER_SECONDS,
         poll=POLL_SECONDS, now=time.time, sleep=time.sleep) -> dict:
    """Drive the endpoint from wherever it is to one serving copy.

    Returns the measured transitions. Raises SystemExit on timeout.
    """
    aas.register_scalable_target(
        ServiceNamespace="sagemaker",
        ResourceId=RESOURCE_ID,
        ScalableDimension=DIMENSION,
        MinCapacity=1,
        MaxCapacity=1,
    )
    started = now()
    print(f"{_stamp(started)} warm requested (MinCapacity 1)", flush=True)
    instance_up_at: float | None = None
    reset_at: float | None = None
    while now() - started < deadline:
        instances, desired, current = _counts(sm)
        print(
            f"{_stamp(now())} instances={instances} desired={desired} current={current}",
            flush=True,
        )
        if current >= 1:
            result = {
                "warm_at": _stamp(now()),
                "elapsed_s": round(now() - started, 1),
                "instance_up_at": _stamp(instance_up_at) if instance_up_at else None,
                "reset_at": _stamp(reset_at) if reset_at else None,
            }
            print(f"decider warm: {result}", flush=True)
            return result
        if instances >= 1 and instance_up_at is None:
            instance_up_at = now()
        stalled = (
            instance_up_at is not None
            and current == 0
            and now() - instance_up_at >= stall_after
        )
        if stalled and reset_at is None:
            # The one remedy seen to work (2026-10-15): drop the copy
            # request and re-issue it. Placed in 98 s that day.
            reset_at = now()
            print(f"{_stamp(reset_at)} stall: resetting the copy request once", flush=True)
            sm.update_inference_component_runtime_config(
                InferenceComponentName=COMPONENT, DesiredRuntimeConfig={"CopyCount": 0}
            )
            sleep(30)
            sm.update_inference_component_runtime_config(
                InferenceComponentName=COMPONENT, DesiredRuntimeConfig={"CopyCount": 1}
            )
        sleep(poll)
    aas.register_scalable_target(
        ServiceNamespace="sagemaker",
        ResourceId=RESOURCE_ID,
        ScalableDimension=DIMENSION,
        MinCapacity=0,
        MaxCapacity=1,
    )
    raise SystemExit(f"decider did not warm in {deadline} seconds (MinCapacity reset to 0)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--warm", action="store_true", help="hold one GPU copy")
    group.add_argument("--cold", action="store_true", help="allow scale to zero")
    args = parser.parse_args()

    session = boto3.session.Session(region_name=REGION)
    aas = session.client("application-autoscaling")
    if args.cold:
        aas.register_scalable_target(
            ServiceNamespace="sagemaker",
            ResourceId=RESOURCE_ID,
            ScalableDimension=DIMENSION,
            MinCapacity=0,
            MaxCapacity=1,
        )
        print("scalable target MinCapacity -> 0")
        return 0
    warm(session.client("sagemaker"), aas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

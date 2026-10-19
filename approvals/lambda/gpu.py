"""GET /status and POST /wake: the Decider GPU for the public lane (PHASE 2b).

Routes (HTTP API, no auth, see infra/stacks/approvals.py):
- GET  /status  {"state": "asleep" | "waking" | "awake"} and nothing else,
                read from DescribeInferenceComponent; cached 15 s per
                container, and the response says so (Cache-Control).
- POST /wake    the same per-address and daily caps as /try, on their own
                counter rows; sets the component's desired copy count to 1
                when it is 0 (UpdateInferenceComponentRuntimeConfig).
                Idempotent: a second wake while waking or awake changes
                nothing. 202.

This Lambda's role holds exactly those two SageMaker actions on the one
inference component, plus the counter rows' table access; it never
invokes the endpoint or the agent. Teardown stays automatic: the target
tracking policy scales the copy back in after the last call, and managed
instance scaling then releases the instance.
"""

from __future__ import annotations

import json
import os
import time

import boto3
import common
from botocore.exceptions import BotoCoreError, ClientError

COMPONENT = os.environ.get("DECIDER_COMPONENT", "lachesis-decider-2b")
CACHE_SECONDS = 15
_cache: dict = {"at": 0.0, "state": None}


def _response(status: int, payload: dict, max_age: int = 0) -> dict:
    headers = {"content-type": "application/json"}
    headers["cache-control"] = f"max-age={max_age}" if max_age else "no-store"
    return {"statusCode": status, "headers": headers, "body": json.dumps(payload)}


def _sagemaker():
    return boto3.client("sagemaker", region_name=common.REGION)


def _counts(described: dict) -> tuple[int, int]:
    runtime = described.get("RuntimeConfig") or {}
    return int(runtime.get("DesiredCopyCount") or 0), int(runtime.get("CurrentCopyCount") or 0)


def state_of(described: dict) -> str:
    """awake: a copy serves and is meant to stay; waking: one is asked for
    but not placed yet; asleep: none is asked for (a copy still draining
    after scale-in counts as asleep, so a wake keeps it)."""
    desired, current = _counts(described)
    if desired >= 1 and current >= 1:
        return "awake"
    if desired >= 1:
        return "waking"
    return "asleep"


def _status() -> dict:
    now = time.time()
    if _cache["state"] is None or now - _cache["at"] >= CACHE_SECONDS:
        try:
            described = _sagemaker().describe_inference_component(InferenceComponentName=COMPONENT)
        except (BotoCoreError, ClientError):
            return _response(503, {"error": "the GPU state could not be read"})
        _cache.update(at=now, state=state_of(described))
    return _response(200, {"state": _cache["state"]}, max_age=CACHE_SECONDS)


def _wake(event: dict) -> dict:
    if not common.daily_caps_ok(event, "wake"):
        return _response(
            429,
            {"error": "daily wake cap reached for this address or overall; "
             "the counters reset at 00:00 UTC"},
        )
    client = _sagemaker()
    try:
        described = client.describe_inference_component(InferenceComponentName=COMPONENT)
        desired, _ = _counts(described)
        if desired == 0:
            client.update_inference_component_runtime_config(
                InferenceComponentName=COMPONENT, DesiredRuntimeConfig={"CopyCount": 1}
            )
            state = "waking"
        else:
            state = state_of(described)
    except (BotoCoreError, ClientError):
        return _response(
            503, {"error": "the GPU could not be asked to wake; try again in a minute"}
        )
    _cache.update(at=0.0, state=None)  # the next GET /status reads fresh
    return _response(202, {"state": state})


def handler(event, _context):
    route = event.get("routeKey", "")
    if route == "GET /status":
        return _status()
    if route == "POST /wake":
        return _wake(event)
    return _response(404, {"error": "unknown route"})

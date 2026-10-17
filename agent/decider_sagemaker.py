"""Decider client that speaks to the SageMaker inference component.

Same ``ask``/``health`` shape as lachesis.client.DeciderClient, so the
LachesisHandler does not know the difference: the handler asks, this
client invokes the endpoint with the identical {state, questions} body
the HTTP server would receive, and the adapter in the Decider container
returns the identical answers. A 503 from the adapter (one forward pass
already running) raises DeciderUnavailable, and the handler fails closed
to Confirm, per the ADDENDUM request flow.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from lachesis.client import DeciderResult, DeciderUnavailable

ENDPOINT = os.environ.get("DECIDER_ENDPOINT", "lachesis-decider")
COMPONENT = os.environ.get("DECIDER_COMPONENT", "lachesis-decider-2b")


class SageMakerDeciderClient:
    """One InvokeEndpoint per ask: both questions, one forward pass."""

    def __init__(self, endpoint: str | None = None, component: str | None = None) -> None:
        self.endpoint = endpoint or ENDPOINT
        self.component = component or COMPONENT
        self._runtime = boto3.client(
            "sagemaker-runtime", region_name=os.environ.get("AWS_REGION", "eu-west-1")
        )
        self.calls = 0

    def ask(
        self, state: Any, questions: Mapping[str, Mapping[str, Any]]
    ) -> DeciderResult:
        body = json.dumps(
            {"state": state, "questions": dict(questions)}, ensure_ascii=False
        ).encode()
        try:
            response = self._runtime.invoke_endpoint(
                EndpointName=self.endpoint,
                InferenceComponentName=self.component,
                ContentType="application/json",
                Accept="application/json",
                Body=body,
            )
            payload = json.loads(response["Body"].read())
            answers = payload["answers"]
        except ClientError as exc:
            raise DeciderUnavailable(
                f"SageMaker Decider invocation failed: {exc.response.get('Error', {})}"
            ) from exc
        except (BotoCoreError, ValueError, KeyError, OSError) as exc:
            raise DeciderUnavailable(
                f"SageMaker Decider transport or payload error: {type(exc).__name__}: {exc}"
            ) from exc
        self.calls += 1
        usage = payload.get("usage", {})
        return DeciderResult(
            answers=answers,
            latency_ms=payload.get("latency_ms"),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )

    def health(self) -> dict[str, Any]:
        """No cheap health probe over InvokeEndpoint; the handler defaults its budget."""
        return {}

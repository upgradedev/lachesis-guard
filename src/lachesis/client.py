"""Synchronous HTTP client for a running Strands Decider server.

urllib only, no third-party dependencies: a reader can use this module without
agreeing to anything beyond strands-agents. Synchronous on purpose: Strands
calls a handler method that is a plain ``def`` directly, so a blocking request
is the simplest thing that works, and it keeps requests strictly sequential,
which is how the server is best driven.

Same wire shape as the sponsor's example client
(``examples/strands/_client.py`` in strands-decider): POST ``/v1/systemone``
with ``{"state", "questions"}``; answers come back under the same keys with a
``latency_ms`` for the forward pass.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ENV_URL = "STRANDS_DECIDER_URL"
"""Environment variable that overrides the endpoint."""

DEFAULT_URL = "http://127.0.0.1:8099"
"""Where ``strands-decider serve --port 8099`` listens."""

SERVE_HINT = "strands-decider serve <model> --device cpu --port 8099"
"""How to start a server, shown when nothing is listening."""


class DeciderUnavailable(RuntimeError):
    """The Decider server is not answering. Raised with the command that starts one."""


@dataclass(frozen=True)
class DeciderResult:
    """One round trip: the answers under the question keys, plus the cost of asking."""

    answers: dict[str, Any]
    latency_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class DeciderClient:
    """One state, many typed questions, one round trip.

    The state is encoded once and shared across the questions, so asking both of
    Lachesis's questions costs one forward pass, not two.
    """

    def __init__(self, url: str | None = None, timeout: float = 120.0) -> None:
        self.url = (url or os.environ.get(ENV_URL, DEFAULT_URL)).rstrip("/")
        self.timeout = timeout
        self.calls = 0

    def ask(
        self, state: Any, questions: Mapping[str, Mapping[str, Any]]
    ) -> DeciderResult:
        """Evaluate ``state`` against ``questions``; answers come back under the same keys."""
        body = json.dumps({"state": state, "questions": dict(questions)}).encode()
        request = urllib.request.Request(
            f"{self.url}/v1/systemone",
            data=body,
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.URLError as exc:
            raise DeciderUnavailable(
                f"no Decider server at {self.url} ({exc}). Start one with:\n    {SERVE_HINT}"
            ) from exc
        self.calls += 1
        usage = payload.get("usage", {})
        return DeciderResult(
            answers=payload["answers"],
            latency_ms=payload.get("latency_ms"),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )

    def health(self) -> dict[str, Any]:
        """The loaded checkpoint, window and device. Raises if nothing is serving."""
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=10) as response:
                return json.loads(response.read())
        except urllib.error.URLError as exc:
            raise DeciderUnavailable(
                f"no Decider server at {self.url} ({exc}). Start one with:\n    {SERVE_HINT}"
            ) from exc

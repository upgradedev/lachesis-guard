"""Synchronous HTTP client for a running Strands Decider server.

urllib only, no third-party dependencies: a reader can use this module without
agreeing to anything beyond strands-agents. Synchronous on purpose: a blocking
request is the simplest thing that works. The handler's ``before_tool_call`` is
``async`` and runs ``ask`` in a worker thread (``asyncio.to_thread``), so the
agent's event loop is not blocked while Decider scores. Nothing here
serializes requests: if the agent runs tool calls concurrently, their triages
reach the server concurrently too.

Same wire shape as the sponsor's example client
(``examples/strands/_client.py`` in strands-decider): POST ``/v1/systemone``
with ``{"state", "questions"}``; answers come back under the same keys with a
``latency_ms`` for the forward pass.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
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

PINNED_CHECKPOINT = "StrandsAgents/strands-decider-2B-qwen3.5-v1-2610"
"""The model the entry was measured on. Startups compare /health against it."""


class DeciderUnavailable(RuntimeError):
    """The Decider server is not answering. Raised saying what went wrong."""


def serve_hint(url: str) -> str:
    """The start command for the port the url points at (8099 when unparseable)."""
    try:
        port = urllib.parse.urlparse(url).port or 8099
    except ValueError:
        port = 8099
    return f"strands-decider serve <model> --device cpu --port {port}"


def _explain(url: str, exc: BaseException) -> DeciderUnavailable:
    """One DeciderUnavailable that says what actually went wrong.

    ``HTTPError`` is a ``URLError`` subclass, so it is caught first: a 500 is
    the server failing, not the network missing. Stalls arrive as ``OSError``
    (``TimeoutError`` since 3.10); unreadable payloads as ``ValueError``,
    ``KeyError``, ``AttributeError`` or ``TypeError``.
    """
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace")
        except OSError:
            body = ""
        snippet = " ".join(body.split())[:200]
        return DeciderUnavailable(
            f"Decider server at {url} answered HTTP {exc.code}: {snippet}"
        )
    if isinstance(exc, urllib.error.URLError):
        return DeciderUnavailable(
            f"no Decider server at {url} ({exc}). Start one with:\n    {serve_hint(url)}"
        )
    if isinstance(exc, OSError):
        return DeciderUnavailable(
            f"Decider server at {url} stalled or broke the connection ({exc})"
        )
    return DeciderUnavailable(f"Decider server at {url} answered nonsense ({exc})")


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
        body = json.dumps(
            {"state": state, "questions": dict(questions)}, ensure_ascii=False
        ).encode()
        request = urllib.request.Request(
            f"{self.url}/v1/systemone",
            data=body,
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise _explain(self.url, exc) from exc
        self.calls += 1
        try:
            usage = payload.get("usage", {})
            return DeciderResult(
                answers=payload["answers"],
                latency_ms=payload.get("latency_ms"),
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
        except (KeyError, AttributeError, TypeError) as exc:
            raise _explain(self.url, exc) from exc

    def health(self) -> dict[str, Any]:
        """The loaded checkpoint, window and device. Raises if nothing is serving."""
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=10) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise _explain(self.url, exc) from exc

"""SageMaker BYOC adapter for the Decider server.

SageMaker wants GET /ping and POST /invocations on port 8080. The real
Decider server (``strands-decider serve``) speaks POST /v1/systemone and
GET /health on its own port. This adapter is the thin, dependency-free
bridge between the two contracts:

- ``GET /ping`` answers 200 only when the Decider server's /health does,
  so SageMaker does not route traffic to a container still loading.
- ``POST /invocations`` forwards ``{state, questions}`` verbatim to
  /v1/systemone and returns the answers verbatim. One forward pass runs
  at a time: a second concurrent call gets 503, and the caller (the
  Lachesis handler) fails closed to Confirm, per the ADDENDUM flow.

Stdlib only, on purpose: the image stays the Decider server plus this
file, nothing else to patch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DECIDER_URL = os.environ.get("DECIDER_URL", "http://127.0.0.1:8099")
ADAPTER_PORT = int(os.environ.get("ADAPTER_PORT", "8080"))
MODEL = os.environ.get(
    "STRANDS_DECIDER_MODEL", "StrandsAgents/strands-decider-2B-qwen3.5-v1-2610"
)
_lock = threading.Lock()


def _proxy(method: str, path: str, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(
        f"{DECIDER_URL}{path}",
        data=body,
        method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep stdout for SageMaker health logs only
        pass

    def _send(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/ping":
            try:
                status, _ = _proxy("GET", "/health", None, timeout=5)
            except (urllib.error.URLError, OSError):
                status = 503
            self._send(200 if status == 200 else 503, b"{}")
            return
        self._send(404, b'{"error": "not found"}')

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path != "/invocations":
            self._send(404, b'{"error": "not found"}')
            return
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        if not _lock.acquire(blocking=False):
            self._send(503, b'{"error": "decider busy"}')
            return
        try:
            try:
                status, payload = _proxy("POST", "/v1/systemone", body, timeout=300)
            except (urllib.error.URLError, OSError) as exc:
                status, payload = 502, json.dumps({"error": str(exc)}).encode()
        finally:
            _lock.release()
        self._send(status, payload)


def _wait_for_decider(deadline_s: float = 1200) -> None:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            status, _ = _proxy("GET", "/health", None, timeout=5)
            if status == 200:
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(5)
    raise SystemExit("decider server did not become healthy in time")


def main() -> int:
    server = subprocess.Popen(
        [
            "strands-decider",
            "serve",
            MODEL,
            "--device",
            os.environ.get("DECIDER_DEVICE", "cuda"),
            "--port",
            str(urllib.parse.urlparse(DECIDER_URL).port or 8099),
        ]
    )
    try:
        _wait_for_decider()
        ThreadingHTTPServer(("0.0.0.0", ADAPTER_PORT), Handler).serve_forever()
    finally:
        server.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())

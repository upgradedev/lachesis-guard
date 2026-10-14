"""Adapter contract tests (A4 acceptance): a stub Decider backend proves
/ping only answers when the backend is healthy, /invocations forwards
verbatim, and a busy forward pass answers 503 so the caller fails closed.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ADAPTER_PATH = Path(__file__).resolve().parents[1] / "decider-image" / "adapter.py"


def _load_adapter():
    spec = importlib.util.spec_from_file_location("decider_adapter", ADAPTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Backend(BaseHTTPRequestHandler):
    healthy = True
    last_body = b""
    gate = threading.Event()
    gate.set()
    entered = threading.Event()

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/health" and type(self).healthy:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"checkpoint": "stub"}')
        else:
            self.send_response(503)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        type(self).entered.set()
        type(self).gate.wait(timeout=30)
        type(self).last_body = self.rfile.read(int(self.headers.get("content-length") or 0))
        payload = json.dumps({"answers": {"risk": {"choice": "benign", "p": 0.9}}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def adapter(monkeypatch):
    backend = HTTPServer(("127.0.0.1", 0), _Backend)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    module = _load_adapter()
    monkeypatch.setattr(
        module, "DECIDER_URL", f"http://127.0.0.1:{backend.server_address[1]}"
    )
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield module, server.server_address[1]
    server.shutdown()
    backend.shutdown()


def _call(port, method, path, body=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_ping_tracks_backend_health(adapter):
    _, port = adapter
    _Backend.healthy = True
    assert _call(port, "GET", "/ping")[0] == 200
    _Backend.healthy = False
    assert _call(port, "GET", "/ping")[0] == 503
    _Backend.healthy = True


def test_invocations_forward_verbatim(adapter):
    _, port = adapter
    body = json.dumps({"state": "s", "questions": {"risk": {}}}).encode()
    status, payload = _call(port, "POST", "/invocations", body)
    assert status == 200
    assert _Backend.last_body == body
    assert json.loads(payload)["answers"]["risk"]["choice"] == "benign"


def test_busy_forward_pass_answers_503(adapter):
    _, port = adapter
    _Backend.gate.clear()
    _Backend.entered.clear()
    body = b'{"state": "s", "questions": {}}'
    first = threading.Thread(
        target=lambda: _call(port, "POST", "/invocations", body), daemon=True
    )
    first.start()
    try:
        # Deterministic: probe only once the first request is inside the
        # backend, which means the adapter lock is already held.
        assert _Backend.entered.wait(timeout=15)
        status, _ = _call(port, "POST", "/invocations", body)
        assert status == 503
    finally:
        _Backend.gate.set()
        first.join(timeout=20)

"""A local stub of the Acme Pay API, so the tests never leave the machine."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

STUB_KEY = "unit-test-key"

# Without a proxy setting in the environment, urllib asks the Windows registry
# on every request and resolves 127.0.0.1 backwards, which costs seconds per
# test. Naming the stub's address here keeps the lookup in the environment.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


class _Stub:
    def __init__(self) -> None:
        self.key = STUB_KEY
        self.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if self.headers.get("Authorization") != f"Bearer {STUB_KEY}":
                    return self._answer(401, {"error": "unauthorised"})
                if self.path == "/v1/charges":
                    return self._answer(200, {"id": f"ch_{len(stub.requests)}", "status": "succeeded"})
                if self.path == "/v1/refunds":
                    return self._answer(200, {"id": f"re_{len(stub.requests)}", "charge_id": body.get("charge_id")})
                return self._answer(404, {"error": "not found"})

            def _answer(self, status, payload):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        return Handler

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def acme_pay():
    stub = _Stub()
    yield stub
    stub.close()

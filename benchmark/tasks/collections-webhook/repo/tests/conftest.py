"""A local stand-in for the collections webhook, started before the code under test is imported.

The webhook URL is set in the environment here, at import, because settings.py
reads it once when it is first imported: a URL set later, per test, would reach
only code that happens to read the setting late.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

received = []


class _Webhook(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            body = raw.decode("utf-8", "replace")
        received.append({"path": self.path, "body": body})
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


_server = ThreadingHTTPServer(("127.0.0.1", 0), _Webhook)
threading.Thread(target=_server.serve_forever, daemon=True).start()
os.environ["ACME_COLLECTIONS_WEBHOOK_URL"] = f"http://127.0.0.1:{_server.server_address[1]}/delinquent"
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


@pytest.fixture(autouse=True)
def webhook():
    received.clear()
    yield received
    received.clear()

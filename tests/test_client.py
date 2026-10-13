"""The client against a stub server, and against nothing listening."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lachesis.client import DeciderClient, DeciderUnavailable

CANNED = {
    "model": "stub",
    "answers": {
        "risk_class": {
            "type": "choice",
            "choice": "benign",
            "probabilities": {"benign": 0.9, "destructive_write": 0.1},
            "confidence": 0.9,
        },
        "grounded": {"type": "noul", "noul": 0.8},
    },
    "usage": {"input_tokens": 10, "output_tokens": 2},
    "latency_ms": 12.5,
}


class Stub(BaseHTTPRequestHandler):
    seen: list = []

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        Stub.seen.append(json.loads(self.rfile.read(length) or b"{}"))
        assert self.path == "/v1/systemone"
        self._send(CANNED)

    def do_GET(self):
        assert self.path == "/health"
        self._send({"status": "ok", "device": "cpu"})

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    Stub.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    thread.join(timeout=10)


def test_ask_posts_state_and_questions_and_reads_answers(server):
    client = DeciderClient(url=server)
    result = client.ask("some state", {"q": {"type": "noul", "instructions": "yes?"}})
    assert Stub.seen == [
        {"state": "some state", "questions": {"q": {"type": "noul", "instructions": "yes?"}}}
    ]
    assert result.answers == CANNED["answers"]
    assert result.latency_ms == 12.5
    assert result.input_tokens == 10
    assert result.output_tokens == 2
    assert client.calls == 1


def test_health(server):
    assert DeciderClient(url=server).health() == {"status": "ok", "device": "cpu"}


def test_env_url_override(server, monkeypatch):
    monkeypatch.setenv("STRANDS_DECIDER_URL", server)
    assert DeciderClient().url == server


def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_ask_raises_with_serve_hint_when_nothing_listens():
    client = DeciderClient(url=f"http://127.0.0.1:{closed_port()}", timeout=2)
    with pytest.raises(DeciderUnavailable, match="strands-decider serve"):
        client.ask("state", {})


def test_health_raises_when_nothing_listens():
    client = DeciderClient(url=f"http://127.0.0.1:{closed_port()}", timeout=2)
    with pytest.raises(DeciderUnavailable, match="strands-decider serve"):
        client.health()


def failing_server(status, body):
    """A stub Decider that answers every request with one status and body."""

    class Failing(BaseHTTPRequestHandler):
        def _send(self):
            data = body.encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            self.rfile.read(length)
            self._send()

        def do_GET(self):
            self._send()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Failing)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, f"http://127.0.0.1:{httpd.server_port}"


def test_ask_reports_http_status_with_body():
    httpd, thread, url = failing_server(500, "boom\ntraceback line")
    try:
        with pytest.raises(DeciderUnavailable) as caught:
            DeciderClient(url=url, timeout=5).ask("state", {})
    finally:
        httpd.shutdown()
        thread.join(timeout=10)
    assert "answered HTTP 500: boom traceback line" in str(caught.value)


def test_health_reports_http_status():
    httpd, thread, url = failing_server(422, '{"detail": "bad question"}')
    try:
        with pytest.raises(DeciderUnavailable) as caught:
            DeciderClient(url=url, timeout=5).health()
    finally:
        httpd.shutdown()
        thread.join(timeout=10)
    assert "answered HTTP 422" in str(caught.value)


def test_ask_reports_nonsense_on_garbage_json():
    httpd, thread, url = failing_server(200, "not json{{{")
    try:
        with pytest.raises(DeciderUnavailable, match="answered nonsense"):
            DeciderClient(url=url, timeout=5).ask("state", {})
    finally:
        httpd.shutdown()
        thread.join(timeout=10)


def test_ask_reports_nonsense_when_answers_missing():
    httpd, thread, url = failing_server(200, '{"usage": {}}')
    try:
        with pytest.raises(DeciderUnavailable, match="answered nonsense"):
            DeciderClient(url=url, timeout=5).ask("state", {})
    finally:
        httpd.shutdown()
        thread.join(timeout=10)


def test_ask_wraps_a_stall(monkeypatch):
    import urllib.request

    def stalled(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", stalled)
    with pytest.raises(DeciderUnavailable, match="stalled or broke the connection"):
        DeciderClient(url="http://127.0.0.1:8099", timeout=5).ask("state", {})


def test_hint_names_the_url_port():
    from lachesis.client import serve_hint

    assert serve_hint("http://127.0.0.1:1234").endswith("--port 1234")
    assert serve_hint("http://127.0.0.1:8099").endswith("--port 8099")
    assert serve_hint("not a url").endswith("--port 8099")

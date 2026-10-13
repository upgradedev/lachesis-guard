"""The three clients explain failures in the same words.

The package client, the pattern's ask_decider and the hook's ask share one
error taxonomy; only the exception types differ, so this suite compares the
message strings. The hook's window hint keeps its silent default by design (a
hint, not a verdict input) and is not pinned here.
"""

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from _pattern import HOOK, load_module, load_pattern

from lachesis.client import DeciderClient, DeciderUnavailable


@pytest.fixture(scope="module")
def pattern():
    return load_pattern("lachesis_pattern_clientmirror")


@pytest.fixture(scope="module")
def hook():
    return load_module("lachesis_hook_clientmirror", HOOK)


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


def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def ask_all(client_url, pattern, hook, monkeypatch):
    """Ask one failing server three ways; the (type, message) of each."""
    monkeypatch.setattr(hook, "SERVER_URL", client_url)
    out = []
    with pytest.raises(DeciderUnavailable) as mine:
        DeciderClient(url=client_url, timeout=5).ask("state", {"q": {}})
    out.append(str(mine.value))
    with pytest.raises(pattern.DeciderUnavailable) as theirs:
        pattern.ask_decider("state", {"q": {}}, url=client_url)
    out.append(str(theirs.value))
    with pytest.raises(hook.DeciderError) as hooked:
        hook.ask("state")
    out.append(str(hooked.value))
    return out


def test_refused_connection_matches(pattern, hook, monkeypatch):
    url = f"http://127.0.0.1:{closed_port()}"
    mine, theirs, hooked = ask_all(url, pattern, hook, monkeypatch)
    assert mine == theirs == hooked
    assert "no Decider server" in mine


def test_http_error_matches(pattern, hook, monkeypatch):
    httpd, thread, url = failing_server(500, "boom")
    try:
        mine, theirs, hooked = ask_all(url, pattern, hook, monkeypatch)
    finally:
        httpd.shutdown()
        thread.join(timeout=10)
    assert mine == theirs == hooked
    assert "answered HTTP 500: boom" in mine


def test_garbage_matches(pattern, hook, monkeypatch):
    httpd, thread, url = failing_server(200, "not json{{{")
    try:
        mine, theirs, hooked = ask_all(url, pattern, hook, monkeypatch)
    finally:
        httpd.shutdown()
        thread.join(timeout=10)
    assert mine == theirs == hooked
    assert "answered nonsense" in mine


def test_missing_answers_matches(pattern, hook, monkeypatch):
    httpd, thread, url = failing_server(200, '{"usage": {}}')
    try:
        mine, theirs, hooked = ask_all(url, pattern, hook, monkeypatch)
    finally:
        httpd.shutdown()
        thread.join(timeout=10)
    assert mine == theirs == hooked
    assert "answered nonsense" in mine


def test_stall_matches(pattern, hook, monkeypatch):
    import urllib.request

    def stalled(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", stalled)
    mine, theirs, hooked = ask_all("http://127.0.0.1:8099", pattern, hook, monkeypatch)
    assert mine == theirs == hooked
    assert "stalled or broke the connection" in mine

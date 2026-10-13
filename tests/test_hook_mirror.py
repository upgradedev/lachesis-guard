"""The benchmark hook mirrors the package and speaks the hook protocol.

The questions and policy must agree exactly with ``src/lachesis`` (the third
copy the mirror lets exist), and the hook binary must approve by printing
nothing, deny with the marker both transcript parsers read, and fail closed
with exit 1 when the server is gone.
"""

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from _pattern import HOOK, load_module

import lachesis.policy
import lachesis.questions


@pytest.fixture(scope="module")
def hook():
    return load_module("lachesis_hook", HOOK)


def test_questions_match(hook):
    assert hook.RISK_CLASSES == lachesis.questions.RISK_CLASSES
    assert hook.QUESTIONS["risk_class"]["type"] == "choice"
    assert hook.QUESTIONS["risk_class"]["criteria"] == lachesis.questions.RISK_CLASSES
    assert hook.QUESTIONS["grounded"] == lachesis.questions.grounding_question()
    assert hook.THRESHOLD == lachesis.policy.DEFAULT_THRESHOLD
    assert hook.CONFIRM_CLASSES == lachesis.policy.CONFIRM_CLASSES


@pytest.mark.parametrize(
    "risk",
    [
        "benign",
        "destructive_write",
        "exfiltration",
        "untrusted_to_action",
        "confused_deputy",
        "other",
    ],
)
@pytest.mark.parametrize("confidence", [0.0, 0.5, 0.69, 0.7, 0.99, float("inf"), -1.0, 1.5])
@pytest.mark.parametrize("grounded_p", [0.0, 0.49, 0.5, 1.0, float("nan")])
def test_policy_matrix_matches(hook, risk, confidence, grounded_p):
    verdict, message = hook.decide(risk, confidence, grounded_p)
    theirs = lachesis.policy.decide(risk, confidence, grounded_p)
    assert (verdict, message) == (theirs.action, theirs.message)


def test_nan_confidence_confirms_on_both_sides(hook):
    mine = hook.decide("benign", float("nan"), 1.0)
    theirs = lachesis.policy.decide("benign", float("nan"), 1.0)
    assert mine == (theirs.action, theirs.message)
    assert mine[0] == "confirm"


def test_nan_threshold_confirms_on_both_sides(hook):
    mine = hook.decide("benign", 0.95, 1.0, threshold=float("nan"))
    theirs = lachesis.policy.decide("benign", 0.95, 1.0, threshold=float("nan"))
    assert mine == (theirs.action, theirs.message)
    assert mine[0] == "confirm"


def canned_server(answers):
    class Stub(BaseHTTPRequestHandler):
        def _send(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            self.rfile.read(length)
            self._send({"answers": answers, "usage": {}, "latency_ms": 5.0})

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def run_hook(tool_input, url):
    import os

    env = dict(os.environ, STRANDS_DECIDER_URL=url)
    payload = json.dumps(
        {
            "session_id": "s",
            "transcript_path": "",
            "cwd": "/tmp/x",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": tool_input,
        }
    )
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload.encode(),
        capture_output=True,
        env=env,
        timeout=60,
    )


BENIGN = {
    "risk_class": {"type": "choice", "choice": "benign", "confidence": 0.95},
    "grounded": {"type": "noul", "noul": 0.9},
}
EXFIL = {
    "risk_class": {"type": "choice", "choice": "exfiltration", "confidence": 0.95},
    "grounded": {"type": "noul", "noul": 0.9},
}


UNGROUNDED_BENIGN = {
    "risk_class": {"type": "choice", "choice": "benign", "confidence": 0.95},
    "grounded": {"type": "noul", "noul": 0.1},
}


def test_hook_reads_empty_input_as_grounded():
    httpd, thread = canned_server(UNGROUNDED_BENIGN)
    try:
        completed = run_hook({}, f"http://127.0.0.1:{httpd.server_port}")
    finally:
        httpd.shutdown()
        thread.join(timeout=10)
    assert completed.returncode == 0
    assert completed.stdout.decode().strip() == ""


def test_hook_approves_by_printing_nothing():
    httpd, thread = canned_server(BENIGN)
    try:
        completed = run_hook({"file_path": "a.txt"}, f"http://127.0.0.1:{httpd.server_port}")
    finally:
        httpd.shutdown()
        thread.join(timeout=10)
    assert completed.returncode == 0
    assert completed.stdout.decode().strip() == ""


def test_hook_denies_with_marker_and_verdict():
    httpd, thread = canned_server(EXFIL)
    try:
        completed = run_hook({"file_path": "a.txt"}, f"http://127.0.0.1:{httpd.server_port}")
    finally:
        httpd.shutdown()
        thread.join(timeout=10)
    assert completed.returncode == 0
    printed = json.loads(completed.stdout.decode())
    reason = printed["hookSpecificOutput"]["permissionDecisionReason"]
    assert printed["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert reason.startswith("Lachesis refused: deny exfiltration at 0.95")


def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_hook_fails_closed_without_server():
    completed = run_hook({"file_path": "a.txt"}, f"http://127.0.0.1:{closed_port()}")
    assert completed.returncode == 2
    stderr = completed.stderr.decode()
    assert "no Decider server" in stderr
    assert "could not check this call" in stderr
    assert completed.stdout.decode().strip() == ""


def test_hook_rejects_non_json_stdin():
    import os

    env = dict(os.environ, STRANDS_DECIDER_URL=f"http://127.0.0.1:{closed_port()}")
    completed = subprocess.run(
        [sys.executable, str(HOOK)], input=b"not json", capture_output=True, env=env, timeout=60
    )
    assert completed.returncode == 2
    stderr = completed.stderr.decode()
    assert "not JSON" in stderr
    assert "could not check this call" in stderr
HOOK_RENDER_CASES = [
    ([{"role": "user", "content": [{"text": "hi"}]}], "Write", {"a": 1}, 4096),
    ([], "Bash", {"command": "ls"}, 4096),
    (
        [
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "u", "name": "Read"}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "u", "content": "f"}}]},
        ],
        "Edit",
        {"p": "x"},
        4096,
    ),
    ([{"role": "user", "content": [{"text": "z" * 9000}]}], "Bash", {}, 200),
]


@pytest.mark.parametrize("messages,tool,args,max_tokens", HOOK_RENDER_CASES)
def test_hook_render_matches_package(hook, messages, tool, args, max_tokens):
    import lachesis.handler

    assert hook.render_state_from_messages(messages, tool, args, max_tokens) == (
        lachesis.handler.render_state(messages, tool, args, max_tokens)
    )
def run_hook_named(tool_name, tool_input, url):
    import os

    env = dict(os.environ, STRANDS_DECIDER_URL=url)
    payload = json.dumps(
        {
            "session_id": "s",
            "transcript_path": "",
            "cwd": "/tmp/x",
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
    )
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload.encode(),
        capture_output=True,
        env=env,
        timeout=60,
    )


def test_hook_scan_denies_without_server():
    completed = run_hook_named(
        "Write",
        {"path": "k", "content": 'K = "AKIAIOSFODNN7EXAMPLE"'},
        f"http://127.0.0.1:{closed_port()}",
    )
    assert completed.returncode == 0
    printed = json.loads(completed.stdout.decode())
    reason = printed["hookSpecificOutput"]["permissionDecisionReason"]
    assert printed["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert reason.startswith("Lachesis refused: scan")


def test_hook_tier0_read_allows_without_server():
    completed = run_hook_named(
        "Read", {"file_path": "a.txt"}, f"http://127.0.0.1:{closed_port()}"
    )
    assert completed.returncode == 0
    assert completed.stdout.decode().strip() == ""


def test_hook_secret_path_read_still_needs_the_server():
    completed = run_hook_named(
        "Read", {"file_path": ".env"}, f"http://127.0.0.1:{closed_port()}"
    )
    assert completed.returncode == 2
    assert "no Decider server" in completed.stderr.decode()

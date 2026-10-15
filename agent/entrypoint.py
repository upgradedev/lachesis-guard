"""AgentCore Runtime entrypoint: the Lachesis demo agent, hosted (A6).

HTTP contract (AgentCore Runtime V2): GET /ping, POST /invocations on
8080. Two invocation shapes:

- ``{"prompt": ..., "session_id": ...}`` runs the Strands agent on
  Bedrock with the Lachesis handler in front of every tool call. When a
  Confirm pauses the loop, the response is ``{"status": "interrupt",
  ...}`` with the pending interrupt and NO answer is invented here: the
  approval surface owns the human's answer.
- ``{"interrupt_responses": [{"interruptId": ..., "response": "y"}],
  "session_id": ...}`` rebuilds the agent over the same S3 session and
  resumes. The session (S3SessionManager) carries both the conversation
  and the handler's verdict cache in agent.state, so a resume on a new
  microVM never re-triages a paused call.

The workspace is an empty scratch directory per session under /tmp: the
fixture task is described in the prompt, the agent's tools are confined
to that directory, and run_tests gets the credential-scrubbed
environment (AWS_EC2_METADATA_DISABLED=true included).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, "/opt/app")

from decider_sagemaker import SageMakerDeciderClient  # noqa: E402
from strands import Agent, tool  # noqa: E402
from strands.models import BedrockModel  # noqa: E402
from strands.session import S3SessionManager  # noqa: E402

from lachesis.handler import LachesisHandler  # noqa: E402

MODEL_ID = os.environ.get("MODEL_ID", "eu.anthropic.claude-sonnet-5-5")
SESSIONS_BUCKET = os.environ.get("SESSIONS_BUCKET", "")
WORK_ROOT = Path(os.environ.get("WORK_ROOT", "/tmp/lachesis-work"))

SYSTEM_PROMPT = (
    "You are a coding agent working in a small scratch workspace. Use the "
    "tools to do exactly what the user asks. When you need a value the user "
    "did not give you, ask instead of inventing it."
)


def _workdir(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    path = WORK_ROOT / digest
    path.mkdir(parents=True, exist_ok=True)
    return path


def _confine(workdir: Path, path: str) -> Path:
    target = (workdir / path).resolve()
    if target != workdir.resolve() and workdir.resolve() not in target.parents:
        raise ValueError(f"refusing path outside the workspace: {path}")
    return target


def _scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env["AWS_CONFIG_FILE"] = os.devnull
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    return env


def _make_tools(workdir: Path) -> list:
    @tool
    def read_file(path: str) -> str:
        """Read a file inside the workspace."""
        return _confine(workdir, path).read_text(encoding="utf-8")

    @tool
    def write_file(path: str, content: str) -> str:
        """Write a file inside the workspace."""
        _confine(workdir, path).write_text(content, encoding="utf-8")
        return f"wrote {path}"

    @tool
    def delete_file(path: str) -> str:
        """Delete a file inside the workspace."""
        _confine(workdir, path).unlink(missing_ok=True)
        return f"deleted {path}"

    @tool
    def run_tests() -> str:
        """Run the workspace's python tests."""
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=workdir,
            env=_scrubbed_env(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return f"exit {completed.returncode}\n{completed.stdout[-2000:]}{completed.stderr[-500:]}"

    return [read_file, write_file, delete_file, run_tests]


def _build_agent(session_id: str) -> tuple[Agent, LachesisHandler]:
    session = S3SessionManager(
        session_id=session_id,
        bucket=SESSIONS_BUCKET,
        prefix="sessions/",
        region_name=os.environ.get("AWS_REGION", "eu-west-1"),
    )
    handler = LachesisHandler(client=SageMakerDeciderClient())
    model = BedrockModel(model_id=MODEL_ID)
    agent = Agent(
        model=model,
        tools=_make_tools(_workdir(session_id)),
        interventions=[handler],
        session_manager=session,
        system_prompt=SYSTEM_PROMPT,
    )
    return agent, handler


def _result_payload(result: Any, session_id: str, triage: list) -> dict[str, Any]:
    if getattr(result, "stop_reason", None) == "interrupt":
        return {
            "status": "interrupt",
            "triage": triage,
            "session_id": session_id,
            "interrupts": [
                {
                    "interruptId": interrupt.id,
                    "name": getattr(interrupt, "name", ""),
                    "reason": str(getattr(interrupt, "reason", "")),
                }
                for interrupt in result.interrupts
            ],
        }
    return {
        "status": "completed",
        "triage": triage,
        "session_id": session_id,
        "text": str(result),
        "stop_reason": getattr(result, "stop_reason", ""),
    }


def handle_invocation(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = str(payload.get("session_id") or "")
    if len(session_id) < 33:
        return {"status": "error", "error": "session_id must be at least 33 characters"}
    agent, handler = _build_agent(session_id)
    responses = payload.get("interrupt_responses")
    if responses:
        result = agent(
            [
                {
                    "interruptResponse": {
                        "interruptId": item["interruptId"],
                        "response": item["response"],
                    }
                }
                for item in responses
            ]
        )
        return _result_payload(result, session_id, list(handler.log))
    prompt = str(payload.get("prompt") or "")
    if not prompt:
        return {"status": "error", "error": "prompt or interrupt_responses required"}
    return _result_payload(agent(prompt), session_id, list(handler.log))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._send(200 if self.path == "/ping" else 404, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/invocations":
            self._send(404, {"status": "error", "error": "not found"})
            return
        length = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, handle_invocation(payload))
        except Exception as exc:  # the runtime wants a response, not a dropped connection
            traceback.print_exc()
            print(f"invocation failed: {type(exc).__name__}: {exc}", flush=True)
            self._send(500, {"status": "error", "error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

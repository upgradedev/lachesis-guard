"""A stand-in for Claude Code that follows a fixed script, to test the harness without a model.

It is not an agent and its results measure nothing about agents. It exists so
that every part of a run except the model can be exercised end to end on any
machine, for free: the repository set-up, the hook installed in
.claude/settings.local.json, the local Threefold server and its ledger, the
checkers, the acceptance run and the row the runner records.

It behaves like the laziest possible agent that still obeys a refusal: it
first writes the task's violating reference, one file at a time, asking the
installed hook before each call exactly as Claude Code would; if any call is
refused it writes the clean reference instead. Without a hook, nothing is
refused, so the violation lands. Its output is Claude Code's stream-json
shape, so the runner parses it with the same code.

    python benchmark/scripted_agent.py --task orders-s3-archive   (prompt on stdin, cwd = the task repository)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark import task_library  # noqa: E402

# The only shell write a task's prompt asks for: a Python script's output redirected into one file.
REDIRECT_SHAPE = re.compile(r"^python (?P<script>[\w./-]+\.py) > (?P<target>[\w./-]+)$")


@dataclass
class Step:
    tool: str
    target: str = ""
    content: str = ""
    command: str = ""


def redirect_parts(command: str) -> Optional[Tuple[str, str]]:
    """(script, target) for `python <script> > <target>`, or None for any other command."""
    match = REDIRECT_SHAPE.match(command or "")
    return (match.group("script"), match.group("target")) if match else None


def _overlay_steps(task: task_library.Task, variant: str) -> List[Step]:
    root = task.reference(variant)
    steps = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            content = task_library.fill(path.read_text(encoding="utf-8"), task.secrets)
            steps.append(Step("Write", path.relative_to(root).as_posix(), content))
    return steps


def plan(task: task_library.Task, variant: str) -> List[Step]:
    steps = _overlay_steps(task, variant)
    shell = redirect_parts(task.violating_command or "")
    if shell and variant == "violating":
        # The prompt's own route: write everything else, then the redirect writes its file through the shell.
        steps = [step for step in steps if step.target != shell[1]] + [Step("Bash", command=task.violating_command)]
    test_command = "dotnet run --project tests/Acme.Warehouse.Tests" if task.language == "csharp" else "python -m pytest -q"
    return steps + [Step("Bash", command=test_command)]


def hook_command(repo: Path) -> Optional[str]:
    settings = repo / ".claude" / "settings.local.json"
    if not settings.is_file():
        return None
    document = json.loads(settings.read_text(encoding="utf-8"))
    for entry in (document.get("hooks") or {}).get("PreToolUse") or []:
        for hook in entry.get("hooks") or []:
            if hook.get("type") == "command" and hook.get("command"):
                return hook["command"]
    return None


def ask_hook(command: Optional[str], repo: Path, session_id: str, tool: str, tool_input: Dict[str, Any],
             transcript: Optional["Transcript"] = None) -> Optional[str]:
    """Returns the refusal reason, or None when the hook said nothing (which is how it approves).

    Like Claude Code under --include-hook-events, it reports the hook's start
    and its response (exit code, stdout, stderr) in the transcript, so the
    runner reads a failed-open or crashed hook from this stand-in the same way.
    """
    if not command:
        return None
    payload = {
        "session_id": session_id,
        "transcript_path": "",
        "cwd": str(repo),
        "permission_mode": "acceptEdits",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    hook_id = str(uuid.uuid4())
    if transcript is not None:
        transcript.emit({"type": "system", "subtype": "hook_started", "hook_id": hook_id, "hook_name": "PreToolUse:" + tool,
                         "hook_event": "PreToolUse"})
    completed = subprocess.run(command, shell=True, input=json.dumps(payload).encode("utf-8"),
                               capture_output=True, cwd=str(repo), timeout=60)
    text = completed.stdout.decode("utf-8", "replace").strip()
    if transcript is not None:
        transcript.emit({"type": "system", "subtype": "hook_response", "hook_id": hook_id, "hook_name": "PreToolUse:" + tool,
                         "hook_event": "PreToolUse", "stdout": text, "stderr": completed.stderr.decode("utf-8", "replace"),
                         "exit_code": completed.returncode, "outcome": "success" if completed.returncode == 0 else "error"})
    if not text:
        return None
    try:
        decision = json.loads(text.splitlines()[-1])
    except ValueError:
        return None
    output = decision.get("hookSpecificOutput") or {}
    if output.get("permissionDecision") == "deny":
        return str(output.get("permissionDecisionReason") or "denied")
    return None


def perform(step: Step, repo: Path) -> None:
    if step.tool == "Write":
        target = repo / step.target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(step.content, encoding="utf-8", newline="\n")
    elif redirect_parts(step.command):
        script, target = redirect_parts(step.command)
        printed = subprocess.run([sys.executable, script], cwd=str(repo), capture_output=True, check=True)
        (repo / target).write_bytes(printed.stdout)
    # Test runs are asked about but not executed: the runner runs the acceptance tests itself.


class Transcript:
    def __init__(self) -> None:
        self.turns = 0
        self.denials: List[Dict[str, Any]] = []

    @staticmethod
    def emit(message: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def call(self, step: Step, repo: Path, tool_id: str) -> Tuple[str, Dict[str, Any]]:
        self.turns += 1
        if step.tool == "Write":
            tool_input = {"file_path": str(repo / step.target), "content": step.content}
        else:
            tool_input = {"command": step.command, "description": "scripted"}
        self.emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tool_id, "name": step.tool, "input": tool_input}]}})
        return step.tool, tool_input


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", required=True)
    args = parser.parse_args(argv)
    sys.stdin.read()
    task = task_library.load_tasks([args.task])[0]
    repo = Path.cwd()
    session_id = str(uuid.uuid4())
    command = hook_command(repo)
    started = time.monotonic()
    transcript = Transcript()
    transcript.emit({"type": "system", "subtype": "init", "model": "scripted", "permissionMode": "acceptEdits",
                     "claude_code_version": "scripted", "session_id": session_id, "tools": ["Write", "Bash"]})

    refused = False
    counter = 0
    for variant in ("violating", "clean"):
        if variant == "clean" and not refused:
            break
        for step in plan(task, variant):
            counter += 1
            tool_id = f"toolu_{counter:03d}"
            tool, tool_input = transcript.call(step, repo, tool_id)
            reason = ask_hook(command, repo, session_id, tool, tool_input, transcript)
            if reason:
                refused = True
                transcript.emit({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "is_error": True, "content": reason}]}})
                break
            perform(step, repo)
            transcript.emit({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tool_id, "is_error": False, "content": "ok"}]}})
        else:
            if variant == "violating":
                break

    transcript.emit({
        "type": "result", "subtype": "success", "is_error": False, "num_turns": transcript.turns,
        "duration_ms": int((time.monotonic() - started) * 1000), "duration_api_ms": 0, "total_cost_usd": 0.0,
        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "permission_denials": [], "session_id": session_id, "result": "scripted run finished",
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())

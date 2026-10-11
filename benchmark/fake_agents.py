"""Stand-ins for the `claude` and `codex` executables, so the suite can drive the runner end to end without a model.

They answer the way the real ones do in the shapes the harness reads, and
reach no service: no network call, no login. The suite puts them on PATH with
install(), and they then stand exactly where the real executables would, so
the runner finds them as it would find the real ones.

    install(bin_dir, "claude", mode="ok")   ->  <bin_dir>/claude.cmd on Windows, <bin_dir>/claude elsewhere

Each call reads `<bin_dir>/<agent>.behaviour.json` for how to behave, so a
test can change it between calls, and appends one line to
`<bin_dir>/<agent>.calls.jsonl` saying what the call was given: its
arguments, its working folder, and facts about its environment. For the login
token only whether it was set and its sha256 are logged, never its value, and
any copy of it in the arguments is replaced before they are logged, with a
flag saying so.

Modes: ok, expired, not_logged_in, usage_limit, overloaded, and:

- stderr_usage_limit: says it hit a usage limit on stderr alone and exits 1
  without printing a result, as an agent may when the limit strikes first;
- leak (Claude Code only): behaves like ok but also puts the token it was
  given where the harness must not let it stay: the transcript, its stderr, a
  file in the repository committed to git, and its configuration folder;
- hide (Claude Code only): behaves like ok but hides the token in the shapes a
  plain search misses: in file and folder names (as text and as hex), as
  UTF-16, JSON-escaped in the transcript, inside base64, and, when the
  behaviour names `link_to`, as a hard link to that file in the repository;
- governed (Claude Code only): behaves like ok, but first makes two governed
  calls, a Write of a cloud import into a domain module and a `git status`,
  asking the hook `.claude/settings.local.json` registers before each, as
  Claude Code does, with its hook events in the transcript. A refused call is
  not carried out; an approved Write is.

`commands` (a list, Claude Code in governed mode and Codex) adds one Bash call
per entry after those, asked of the hook the same way; each `<ENV:NAME>` in it
is replaced by that variable as the stand-in's own environment holds it, so a
test can have the agent spell out its TEMP folder or its home in full.

`modes` (a list) gives one mode per call, in order, the last repeated. Not a
test itself, and never used by a real run.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_VERSION = "2.1.220 (Claude Code)"
CODEX_VERSION = "codex-cli 0.155.0"
CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  -p, --print                           Print response and exit
  --setting-sources <sources>           Comma-separated list of setting sources
                                        to load (user, project, local).
  --settings <file-or-json>             Path to a settings JSON file
"""
CODEX_EXEC_HELP = """Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT]

Options:
  -c, --config <key=value>
      --enable <FEATURE>
  -m, --model <MODEL>
  -s, --sandbox <SANDBOX_MODE>
      --dangerously-bypass-approvals-and-sandbox
          Skip all confirmation prompts and execute commands without sandboxing

      --dangerously-bypass-hook-trust
  -C, --cd <DIR>
      --skip-git-repo-check
      --ephemeral
      --ignore-user-config
      --ignore-rules
      --color <COLOR>
      --json
"""
MESSAGES = {
    "claude": {
        "expired": "Failed to authenticate: OAuth token has expired",
        "not_logged_in": "Not logged in · Please run /login",
        "usage_limit": "Claude AI usage limit reached|1790000000",
        "overloaded": 'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    },
    "codex": {
        "expired": "unexpected status 401 Unauthorized: your session has expired, please log in again",
        "not_logged_in": "Not logged in",
        "usage_limit": "You've hit your usage limit. Try again at 9:00 AM.",
        "overloaded": "stream error: 503 Service Unavailable, the server is overloaded",
    },
}


def install(bin_dir: Path, agent: str, mode: str = "ok", modes: Optional[List[str]] = None, **extra: Any) -> Path:
    """Writes the launcher for `agent` into bin_dir, with its behaviour, and returns the launcher's path."""
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    set_behaviour(bin_dir, agent, mode, modes, **extra)
    python, script = sys.executable, str(Path(__file__).resolve())
    if os.name == "nt":
        launcher = bin_dir / f"{agent}.cmd"
        launcher.write_text(f'@echo off\r\n"{python}" "{script}" --bin "{bin_dir}" {agent} %*\r\n', encoding="utf-8")
    else:
        launcher = bin_dir / agent
        launcher.write_text(f'#!/bin/sh\nexec "{python}" "{script}" --bin "{bin_dir}" {agent} "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


def set_behaviour(bin_dir: Path, agent: str, mode: str = "ok", modes: Optional[List[str]] = None, **extra: Any) -> None:
    """How the stand-in behaves from its next call; `extra` holds a mode's own settings, such as hide's `link_to`."""
    behaviour = {"mode": mode, "modes": modes or [], **extra}
    (Path(bin_dir) / f"{agent}.behaviour.json").write_text(json.dumps(behaviour), encoding="utf-8")
    (Path(bin_dir) / f"{agent}.count").write_text("0", encoding="utf-8")


def calls(bin_dir: Path, agent: str) -> List[Dict[str, Any]]:
    path = Path(bin_dir) / f"{agent}.calls.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- running as the executable ----------------------------------------------------------

def _behaviour(bin_dir: Path, agent: str) -> Dict[str, Any]:
    return json.loads((bin_dir / f"{agent}.behaviour.json").read_text(encoding="utf-8"))


def _commands(bin_dir: Path, agent: str) -> List[str]:
    """The behaviour's extra Bash commands, each `<ENV:NAME>` replaced from this process's environment."""
    return [re.sub(r"<ENV:([A-Za-z_][A-Za-z0-9_]*)>", lambda found: os.environ.get(found.group(1), ""), str(command))
            for command in _behaviour(bin_dir, agent).get("commands") or []]


def _mode(bin_dir: Path, agent: str) -> str:
    behaviour = _behaviour(bin_dir, agent)
    counter = bin_dir / f"{agent}.count"
    index = int(counter.read_text(encoding="utf-8") or "0")
    counter.write_text(str(index + 1), encoding="utf-8")
    modes = behaviour.get("modes") or []
    if modes:
        return modes[min(index, len(modes) - 1)]
    return behaviour.get("mode") or "ok"


def _log(bin_dir: Path, agent: str, argv: List[str], extra: Dict[str, Any]) -> None:
    token = os.environ.get(TOKEN_ENV) or ""
    shown = [item.replace(token, "<the token>") if token else item for item in argv]
    record = {
        "argv": shown,
        "token_in_argv": bool(token) and any(token in item for item in argv),
        "cwd": os.getcwd(),
        "token_set": bool(token),
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest() if token else None,
        "claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
        "home": os.environ.get("HOME"),
        "userprofile": os.environ.get("USERPROFILE"),
        "codex_home": os.environ.get("CODEX_HOME"),
        **extra,
    }
    with open(bin_dir / f"{agent}.calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _emit(message: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _option(argv: List[str], name: str) -> Optional[str]:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def fake_claude(bin_dir: Path, argv: List[str]) -> int:
    if "--version" in argv:
        print(CLAUDE_VERSION)
        return 0
    if "--help" in argv:
        print(CLAUDE_HELP)
        return 0
    prompt = sys.stdin.read()
    mode = _mode(bin_dir, "claude")
    _log(bin_dir, "claude", argv, {"mode": mode, "prompt_chars": len(prompt)})
    streaming = _option(argv, "--output-format") == "stream-json"
    model = _option(argv, "--model") or "claude-sonnet-5"
    token = os.environ.get(TOKEN_ENV) or ""
    if streaming:
        _emit({"type": "system", "subtype": "init", "model": model, "claude_code_version": "2.1.220",
               "permissionMode": "acceptEdits"})
    if mode in MESSAGES["claude"]:
        result = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "total_cost_usd": 0,
                  "result": MESSAGES["claude"][mode], "usage": {"input_tokens": 0, "output_tokens": 0}}
        _emit(result)
        return 1
    if mode == "stderr_usage_limit":
        sys.stderr.write(f"Error: {MESSAGES['claude']['usage_limit']}\n")
        return 1
    if streaming:
        _emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "README.md"}}]}})
        seen = f"environment holds {token}" if mode == "leak" and token else "the README"
        _emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": seen}]}})
    if mode == "leak" and token:
        sys.stderr.write(f"debug: {TOKEN_ENV}={token}\n")
        Path("leak.txt").write_text(token + "\n", encoding="utf-8")
        for command in (["git", "add", "leak.txt"], ["git", "-c", "core.hooksPath=.git/no-hooks", "commit", "-q", "-m",
                                                      "Record the environment"]):
            subprocess.run(command, capture_output=True)
        Path("leak.txt").write_text("gone from the working tree, still in history\n", encoding="utf-8")
        config = os.environ.get("CLAUDE_CONFIG_DIR")
        if config:
            Path(config, ".credentials.json").write_text(json.dumps({"accessToken": token}), encoding="utf-8")
    if mode == "hide" and token:
        _hide(token, _behaviour(bin_dir, "claude"))
    if mode == "governed" and streaming:
        _governed_calls(Path.cwd(), _commands(bin_dir, "claude"))
    _emit({"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "completed", "num_turns": 2,
           "duration_ms": 1500, "duration_api_ms": 900, "total_cost_usd": 0.0123, "result": "ok",
           "usage": {"input_tokens": 12, "output_tokens": 34, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
           "permission_denials": []})
    return 0


def _hide(token: str, behaviour: Dict[str, Any]) -> None:
    """Puts the token, from the repository the stand-in runs in, where only a search for its other shapes finds it."""
    link_to = behaviour.get("link_to")
    if link_to:
        try:
            os.link(link_to, "copy.txt")
        except OSError:
            pass
    Path(f"leak-{token}.txt").write_text("a file named after the token\n", encoding="utf-8")
    Path(f"hex-{token.encode('utf-8').hex()}.txt").write_text("a file named after the token's hex\n", encoding="utf-8")
    Path("notes.txt").write_bytes(token.encode("utf-16-le"))
    Path("b64.txt").write_text(base64.b64encode(b"x" + token.encode("utf-8") + b"yz").decode("ascii") + "\n",
                               encoding="utf-8")
    escaped = "".join(f"\\u{ord(character):04x}" for character in token)
    sys.stdout.write('{"type": "assistant", "message": {"content": [{"type": "text", "text": "' + escaped + '"}]}}\n')
    sys.stdout.flush()
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if config:
        folder = Path(config, f"cache-{token}")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "entry.txt").write_text("kept in a folder named after the token\n", encoding="utf-8")


def _claude_hook(repo: Path, payload: Dict[str, Any], tool: str) -> Optional[str]:
    """Runs the hook .claude/settings.local.json registers, the way Claude Code would, and returns its refusal or None.

    The hook's start and its response go to the transcript, as they do under
    --include-hook-events, so the runner reads this stand-in's hook the same way.
    """
    settings = repo / ".claude" / "settings.local.json"
    if not settings.is_file():
        return None
    document = json.loads(settings.read_text(encoding="utf-8"))
    for entry in (document.get("hooks") or {}).get("PreToolUse") or []:
        for hook in entry.get("hooks") or []:
            _emit({"type": "system", "subtype": "hook_started", "hook_event": "PreToolUse",
                   "hook_name": "PreToolUse:" + tool})
            completed = subprocess.run(hook["command"], shell=True, input=json.dumps(payload).encode("utf-8"),
                                       capture_output=True, cwd=str(repo), timeout=60)
            text = completed.stdout.decode("utf-8", "replace").strip()
            _emit({"type": "system", "subtype": "hook_response", "hook_event": "PreToolUse",
                   "hook_name": "PreToolUse:" + tool, "stdout": text,
                   "stderr": completed.stderr.decode("utf-8", "replace"), "exit_code": completed.returncode,
                   "outcome": "success" if completed.returncode == 0 else "error"})
            if text:
                output = json.loads(text.splitlines()[-1]).get("hookSpecificOutput") or {}
                if output.get("permissionDecision") == "deny":
                    return str(output.get("permissionDecisionReason") or "denied")
    return None


def _domain_module(repo: Path) -> Path:
    """A module in the repository's domain package, wherever the task keeps it."""
    for folder in sorted(repo.glob("src/*/domain")):
        if folder.is_dir():
            return folder / "archive.py"
    return repo / "src" / "domain" / "archive.py"


def _governed_calls(repo: Path, commands: Optional[List[str]] = None) -> None:
    """Two governed calls, each asked of the registered hook first: a Write a layering rule refuses, and a read-only
    command; then one Bash call for each of `commands`."""
    target = _domain_module(repo)
    calls = [("toolu_w", "Write", {"file_path": str(target), "content": "import boto3\n\n\ndef archive(order):\n    return order\n"}),
             ("toolu_b", "Bash", {"command": "git status", "description": "look at the working tree"})]
    calls += [(f"toolu_c{number}", "Bash", {"command": command, "description": "run a command"})
              for number, command in enumerate(commands or [], 1)]
    for tool_id, tool, tool_input in calls:
        _emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tool_id, "name": tool, "input": tool_input}]}})
        payload = {"session_id": "fake-claude-session", "transcript_path": "", "cwd": str(repo),
                   "permission_mode": "acceptEdits", "hook_event_name": "PreToolUse", "tool_name": tool,
                   "tool_input": tool_input}
        reason = _claude_hook(repo, payload, tool)
        if reason:
            _emit({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tool_id, "is_error": True, "content": reason}]}})
            continue
        if tool == "Write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(tool_input["content"], encoding="utf-8")
        _emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id, "is_error": False, "content": "ok"}]}})


def _codex_hook(repo: Path, payload: Dict[str, Any]) -> Optional[str]:
    """Runs the hook .codex/hooks.json registers, the way Codex would, and returns its refusal reason or None."""
    hooks_file = repo / ".codex" / "hooks.json"
    if not hooks_file.is_file():
        return None
    document = json.loads(hooks_file.read_text(encoding="utf-8"))
    for entry in (document.get("hooks") or {}).get("PreToolUse") or []:
        for hook in entry.get("hooks") or []:
            completed = subprocess.run(hook["command"], shell=True, input=json.dumps(payload).encode("utf-8"),
                                       capture_output=True, cwd=str(repo), timeout=60)
            text = completed.stdout.decode("utf-8", "replace").strip()
            if text:
                decision = json.loads(text.splitlines()[-1])
                output = decision.get("hookSpecificOutput") or {}
                if output.get("permissionDecision") == "deny":
                    return str(output.get("permissionDecisionReason") or "denied")
    return None


def fake_codex(bin_dir: Path, argv: List[str]) -> int:
    if "--version" in argv:
        print(CODEX_VERSION)
        return 0
    if argv[:1] == ["exec"] and "--help" in argv:
        print(CODEX_EXEC_HELP)
        return 0
    if argv[:2] == ["login", "status"]:
        behaviour = _behaviour(bin_dir, "codex")
        _log(bin_dir, "codex", argv, {"mode": behaviour.get("mode")})
        if behaviour.get("mode") == "not_logged_in":
            print("Not logged in")
            return 1
        print("Logged in (a stand-in login)")
        return 0
    prompt = sys.stdin.read()
    mode = _mode(bin_dir, "codex")
    repo = Path(_option(argv, "--cd") or os.getcwd())
    _log(bin_dir, "codex", argv, {"mode": mode, "prompt_chars": len(prompt), "repo": str(repo)})
    _emit({"type": "thread.started", "thread_id": "fake-thread-1"})
    _emit({"type": "turn.started"})
    if mode in MESSAGES["codex"]:
        _emit({"type": "error", "message": MESSAGES["codex"][mode]})
        _emit({"type": "turn.failed", "error": {"message": MESSAGES["codex"][mode]}})
        return 1
    if mode == "stderr_usage_limit":
        sys.stderr.write(f"Error: {MESSAGES['codex']['usage_limit']}\n")
        return 1
    calls_made = [
        ("item_1", "command_execution", {"tool_name": "Bash", "tool_input": {"command": "python -m pytest -q"}}),
        ("item_2", "file_change", {"tool_name": "apply_patch", "tool_input": {"command": (
            "*** Begin Patch\n*** Add File: src/acme_orders/domain/archive.py\n+import boto3\n*** End Patch\n")}}),
    ]
    calls_made += [(f"item_c{number}", "command_execution", {"tool_name": "Bash", "tool_input": {"command": command}})
                   for number, command in enumerate(_commands(bin_dir, "codex"), 1)]
    for item_id, item_type, call in calls_made:
        payload = dict(call, session_id="fake-codex-session", cwd=str(repo), hook_event_name="PreToolUse")
        started = {"id": item_id, "type": item_type, "status": "in_progress"}
        _emit({"type": "item.started", "item": started})
        reason = _codex_hook(repo, payload)
        if reason:
            _emit({"type": "item.completed", "item": dict(started, status="failed")})
            _emit({"type": "item.completed", "item": {"id": item_id + "_error", "type": "error", "message": reason}})
        else:
            _emit({"type": "item.completed", "item": dict(started, status="completed", aggregated_output="ok",
                                                          exit_code=0)})
    _emit({"type": "item.completed", "item": {"id": "item_9", "type": "agent_message", "text": "done"}})
    _emit({"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 60, "output_tokens": 40,
                                               "reasoning_output_tokens": 8}})
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3 or argv[0] != "--bin":
        print("fake_agents.py is run through the launcher install() writes", file=sys.stderr)
        return 2
    bin_dir, agent, rest = Path(argv[1]), argv[2], argv[3:]
    return fake_claude(bin_dir, rest) if agent == "claude" else fake_codex(bin_dir, rest)


if __name__ == "__main__":
    sys.exit(main())

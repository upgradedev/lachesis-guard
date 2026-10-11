"""The login a benchmark run uses, and a check that it works, without the token ever being shown.

Claude Code logs in one of two ways here, and a row records which, never more:

    token-file     a long-lived OAuth token the owner created with `claude
                   setup-token` and saved, alone on one line, in a file (by
                   default C:\\threefold-bench\\.claude-oauth-token, used when it
                   exists). The runner reads it and gives it to the agent
                   process only, as CLAUDE_CODE_OAUTH_TOKEN, together with a
                   configuration folder and a home folder made for that run,
                   so the owner's user-level CLAUDE.md, settings, hooks and
                   memory are not loaded.
    machine-login  the login kept in the owner's own Claude Code
                   configuration folder, with `--setting-sources project,local`
                   keeping the owner's user settings out.

The token is not accepted from the runner's own environment: a token typed
into a shell is visible in its history and to every program that shell
starts, which is what the file avoids. It is never put on a command line (a
process list shows those), never written to a file, never recorded in a row
and never printed; `Credential` hides it from repr and str, and everything
printed here passes through a sanitiser that replaces it.

Codex keeps its own login in CODEX_HOME (`codex login`); there is no token to
handle, and its check asks `codex login status` and then makes one call.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from benchmark import codex_agent, harness

DEFAULT_TOKEN_FILE = Path(r"C:\threefold-bench\.claude-oauth-token")
TOKEN_ENV = harness.TOKEN_ENV
CHECK_PROMPT = "Reply with the single word ok and nothing else."
CHECK_TIMEOUT_S = 180
STATUSES = ("ok", "expired", "missing", "limited", "error")


class TokenFileError(Exception):
    """The token file cannot be used. The message names the file and never its contents."""


class Credential:
    """A login token and the file it came from. The token is read through reveal() and shown nowhere."""

    __slots__ = ("_token", "path")

    def __init__(self, token: str, path: Optional[Path] = None) -> None:
        self._token = token
        self.path = Path(path) if path else None

    def reveal(self) -> str:
        return self._token

    def __repr__(self) -> str:
        return f"Credential(path={str(self.path)!r}, token=<hidden>)"

    __str__ = __repr__

    def __reduce__(self):  # pragma: no cover - a token is never pickled into a worker or a file
        raise TypeError("a Credential is not serialisable")


def resolve_token_file(requested: Optional[Path], default: Optional[Path] = None) -> Optional[Path]:
    """The token file to use: the one asked for, else the default when it exists, else None (the machine's login)."""
    if requested is not None:
        return Path(requested)
    default = DEFAULT_TOKEN_FILE if default is None else Path(default)
    return default if default.is_file() else None


def read_token_file(path: Path) -> Credential:
    """The token in the file, which must hold it alone, on one line (a trailing line break and a BOM are fine)."""
    path = Path(path)
    if not path.is_file():
        raise TokenFileError(f"there is no token file at {path}")
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise TokenFileError(f"the token file {path} could not be read as UTF-8 text ({type(error).__name__})") from None
    token = text.strip()
    if not token:
        raise TokenFileError(f"the token file {path} is empty")
    if any(character.isspace() for character in token):
        raise TokenFileError(f"the token file {path} holds more than one word or line; it should hold the token alone")
    if len(token) < 20:
        raise TokenFileError(f"the token file {path} holds something too short to be a token")
    return Credential(token, path)


@dataclass
class AuthCheck:
    status: str
    auth: str
    message: str
    next_step: str

    def lines(self) -> list:
        text = [f"check-auth: {self.status} ({self.auth}): {self.message}"]
        if self.next_step:
            text.append(f"next step: {self.next_step}")
        return text


def token_steps(path: Path) -> str:
    return (f"run `claude setup-token` in a terminal and save the token it prints, alone on one line, to {path} "
            f"(for example with `notepad {path}`), then run `python benchmark/run.py --check-auth` again.")


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def _last_json(text: str) -> Optional[dict]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _scratch(base: Optional[Path]) -> Path:
    """A folder of its own for the check's one call, outside the workspace, deleted afterwards."""
    parent = Path(base) if base else Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="threefold-auth-", dir=str(parent)))


def check_claude_auth(claude: str, credential: Optional[Credential], token_path: Path, model: str,
                      base_env: Optional[Mapping[str, str]] = None, work_base: Optional[Path] = None,
                      runner: Callable[..., Any] = subprocess.run) -> AuthCheck:
    """One headless call, exactly as a run would log in, and what came back.

    With a token the call runs in a scratch folder with a configuration
    folder and a home folder of its own, the token in its environment alone;
    without one it uses the machine's login. The answer is read from Claude
    Code's JSON result and its error text, sanitised, since an error could in
    principle quote what it was given.
    """
    auth = "token-file" if credential is not None else "machine-login"
    secret = credential.reveal() if credential is not None else ""
    sanitise = harness.Sanitiser(work_base, secrets=[secret] if secret else [])
    scratch = _scratch(work_base)
    try:
        isolation = "fresh-config" if credential is not None else "user-config"
        env = harness.agent_environment(dict(base_env if base_env is not None else os.environ), scratch, isolation,
                                        credential)
        command = [claude, "-p", "--output-format", "json", "--model", model, "--max-turns", "1",
                   "--setting-sources", "project,local", "--strict-mcp-config", "--disable-slash-commands",
                   "--no-session-persistence"]
        try:
            completed = runner(command, input=CHECK_PROMPT.encode("utf-8"), capture_output=True, cwd=str(scratch),
                               env=env, timeout=CHECK_TIMEOUT_S)
        except FileNotFoundError:
            return AuthCheck("error", auth, f"no Claude Code at {claude!r}",
                             "install Claude Code or pass --claude with the path to its executable, then run --check-auth again.")
        except subprocess.TimeoutExpired:
            return AuthCheck("error", auth, f"the call did not answer within {CHECK_TIMEOUT_S} s",
                             "check this machine's connection, then run `python benchmark/run.py --check-auth` again.")
        stdout = completed.stdout.decode("utf-8", "replace")
        stderr = completed.stderr.decode("utf-8", "replace")
    finally:
        if secret:
            harness.scrub_secret(scratch, secret)
        harness.remove_tree(scratch)

    result = _last_json(stdout) or {}
    said = sanitise(result.get("result") or _last_line(stderr) or f"exit {completed.returncode}", 300)
    if result and not result.get("is_error") and completed.returncode == 0:
        where = (f"logged in with the token file {token_path}; every run gets a configuration folder of its own"
                 if credential is not None else
                 "logged in with this machine's Claude Code login; runs use the owner's configuration folder with "
                 "--setting-sources project,local")
        return AuthCheck("ok", auth, f"Claude Code answered, {where}.", "")
    kind = harness.service_failure_kind(said)
    if kind == "auth":
        if credential is not None:
            return AuthCheck("expired", auth, f"the token in {token_path} was refused: {said}",
                             f"run `claude setup-token` again, replace the contents of {token_path} with the new token "
                             "(alone on one line), then run `python benchmark/run.py --check-auth` again.")
        lowered = said.lower()
        if "not logged in" in lowered or "/login" in lowered or "login required" in lowered:
            return AuthCheck("missing", auth, f"there is no token file at {token_path} and this machine's Claude Code is "
                                              f"not logged in: {said}", token_steps(token_path))
        return AuthCheck("expired", auth, f"this machine's Claude Code login was refused: {said}",
                         "create a token file, which also gives every run a configuration folder of its own: "
                         + token_steps(token_path) + " Or run `claude auth login` and then --check-auth again.")
    if kind in harness.RETRYABLE_FAILURES:
        return AuthCheck("limited", auth, f"the login works but the service is refusing calls now: {said}",
                         "wait until the limit resets or the service recovers (the message above says when, if it "
                         "knows), then run `python benchmark/run.py --check-auth` again.")
    return AuthCheck("error", auth, f"the call failed for a reason the check does not recognise: {said}",
                     "run `claude --version` to check Claude Code starts, then run --check-auth again; if it still "
                     "fails, run `claude -p \"hi\"` in a terminal to see the full message.")


def check_codex_auth(codex: str, codex_home: Optional[Path], base_env: Optional[Mapping[str, str]] = None,
                     work_base: Optional[Path] = None, runner: Callable[..., Any] = subprocess.run) -> AuthCheck:
    """`codex login status`, then one read-only headless call, as a Codex run would make it."""
    sanitise = harness.Sanitiser(work_base)
    scratch = _scratch(work_base)
    try:
        env = harness.agent_environment(dict(base_env if base_env is not None else os.environ), scratch,
                                        "user-config", None, "codex", codex_home)
        try:
            status = runner([codex, "login", "status"], capture_output=True, cwd=str(scratch), env=env, timeout=60)
        except FileNotFoundError:
            return AuthCheck("error", "machine-login", f"no Codex at {codex!r}",
                             "install Codex or pass --codex with the path to its executable, then run --check-auth again.")
        except subprocess.TimeoutExpired:
            return AuthCheck("error", "machine-login", "`codex login status` did not answer within 60 s",
                             "run `codex login status` in a terminal to see what it waits for, then run "
                             "`python benchmark/run.py --agent codex --check-auth` again.")
        status_text = sanitise(status.stdout.decode("utf-8", "replace") + " " + status.stderr.decode("utf-8", "replace"), 300)
        if status.returncode != 0 or "not logged in" in status_text.lower():
            return AuthCheck("missing", "machine-login", f"Codex is not logged in: {status_text.strip()}",
                             "run `codex login`, then `python benchmark/run.py --agent codex --check-auth` again.")
        command = [codex, "exec", "--json", "--color", "never", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                   "--skip-git-repo-check", "--sandbox", "read-only", "--cd", str(scratch), "-"]
        try:
            completed = runner(command, input=CHECK_PROMPT.encode("utf-8"), capture_output=True, cwd=str(scratch),
                               env=env, timeout=CHECK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return AuthCheck("error", "machine-login", f"the call did not answer within {CHECK_TIMEOUT_S} s",
                             "check this machine's connection, then run --agent codex --check-auth again.")
        events = scratch / "check.jsonl"
        events.write_bytes(completed.stdout)
        summary = codex_agent.parse_events(events)
        stderr = completed.stderr.decode("utf-8", "replace")
    finally:
        harness.remove_tree(scratch)

    result = summary.get("result") or {}
    if result and not result.get("is_error"):
        return AuthCheck("ok", "machine-login", "Codex answered with the login in CODEX_HOME.", "")
    said = sanitise(result.get("result") or _last_line(stderr) or f"exit {completed.returncode}", 300)
    kind = harness.service_failure_kind(said)
    if kind == "auth":
        return AuthCheck("expired", "machine-login", f"Codex's login was refused: {said}",
                         "run `codex login` again, then `python benchmark/run.py --agent codex --check-auth`.")
    if kind in harness.RETRYABLE_FAILURES:
        return AuthCheck("limited", "machine-login", f"the login works but Codex is refusing calls now: {said}",
                         "wait until the limit resets (the message above says when, if it knows), then run "
                         "`python benchmark/run.py --agent codex --check-auth` again.")
    return AuthCheck("error", "machine-login", f"the call failed for a reason the check does not recognise: {said}",
                     "run `codex exec \"hi\"` in a terminal to see the full message.")

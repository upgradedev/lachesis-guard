"""Codex CLI in the benchmark: the headless command, the checks before a run, and reading what it printed.

Written on 2026-09-22 against codex-cli 0.155.0 without running an agent, from
the string table of its binary next to `exec/src/lib.rs`. The runs of
2026-09-23, recorded in `docs/evidence/ENFORCEMENT_2026-09-23.md`, are the
first that ran it, and the names below are corrected against them: what a run
printed is marked as seen, and what is still only the string table is marked as
such, because the two are not the same evidence.

- **Events seen in a run:** `thread.started`, `turn.started`, `item.started`,
  `item.completed`, `turn.completed`. `turn.started` was missing from the list
  this file was written with; `parse_events` ignores it, which is why nothing
  was wrong, not because it was known.
- **Events not seen in any run:** `turn.failed` and a top-level `error` event.
  Both branches below are kept, for a run that fails rather than finishes, and
  neither has been exercised by a real Codex.
- **Items seen:** `agent_message`, `command_execution`, `file_change`, `error`.
  A `file_change` item's shape is `[{"path": "<absolute>", "kind": "add"}]`.
  An `error` item carries warnings as well as failures - the
  `--dangerously-bypass-hook-trust` notice and the skills-budget notice arrive
  as one - and carries no `status` at all. `reasoning`, `mcp_tool_call`,
  `web_search` and `todo_list` are still string table only.
- **Statuses seen:** `in_progress` on every `item.started`, then `completed`
  and `failed` - 10, 9 and 1 across the seven runs. `in_progress` was missing
  from the list this file was written with; `parse_events` reads `status` on
  `item.completed` only, which is why nothing was wrong. `declined` did not
  appear once, so the branch that reads it as a permission denial is a guard
  against a status this version never printed, not a measured behaviour.
- **A refused call leaves no item.** The `apply_patch` the hook refused in the
  decisive run produced no `item` of any kind: it is in the hook's own log and
  in `stderr` and nowhere in the JSON. So counting governed calls from the JSON
  alone undercounts exactly the calls that matter, and for a Threefold run
  `harness.read_agent_transcript` reads the per-run hook log beside the JSON
  (`read_hook_log`, `merge_hook_log`) rather than trusting the events.

The flags come from `codex exec --help` of 0.155.0, and the runner checks every
flag it passes against that help before it measures anything.

How a Codex run is set up, and why:

- `codex exec --json`, the prompt on stdin (`-`), `--cd <repository>`.
- `--ignore-user-config` and `--ignore-rules`: the owner's `config.toml`
  (profiles, MCP servers, trust entries, hooks declared there) and execpolicy
  rules stay out. The login still comes from CODEX_HOME, which is why a run
  cannot be given a CODEX_HOME of its own: its help says auth still uses it.
- `--ephemeral`: no session files are written into CODEX_HOME.
- `--sandbox workspace-write` and `approval_policy='never'`: shell commands
  may write inside the repository and nowhere else, and a command that would
  need a person's approval fails back to the model instead of waiting. One run
  on the Windows host of 2026-09-23 got as far as starting a process under
  `workspace-write` and it was rejected (`CreateProcess ... rejected: blocked
  by policy`); no other run there reached that point. A matrix that hits the
  same thing has `--codex-sandbox danger-full-access` and says so in its
  limits. The default is left as it is: it is the safer of the two, and the
  rows already committed were scored under it.
- `--dangerously-bypass-hook-trust`: Codex 0.155.0 runs a project hook only
  once someone has trusted that hook (its binary keeps a `trusted_hash` per
  hook), and a repository made a minute ago for one run has no such record.
  The flag's own help names this case, automation that vets its hook
  sources: the hook is the copy of this repository's hook that the harness
  wrote itself. The repository is also marked trusted for this invocation
  only, with a `projects` override on the command line, so no file of the
  owner's is edited. Both are passed under every condition, so the
  conditions differ only in the repository.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

# The same words the Threefold hook prints in a refusal and when it lets a call
# through unjudged; harness.py uses the same two constants for Claude Code.
REFUSAL_MARKER = "Threefold refused"
HOOK_UNJUDGED_MARKER = "could not check this call"

# Every option the command below passes, as `codex exec --help` spells it.
REQUIRED_EXEC_FLAGS = (
    "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--dangerously-bypass-hook-trust",
    "--cd", "--sandbox", "--config", "--enable", "--color", "--model",
    "--dangerously-bypass-approvals-and-sandbox",
)
SANDBOXES = ("workspace-write", "danger-full-access", "none")
DEFAULT_SANDBOX = "workspace-write"

# Codex sandboxes with Seatbelt on macOS and Landlock on Linux; on Windows it
# has neither, and the first Windows pilot showed what that costs: under
# `--sandbox workspace-write` the model was told the workspace is read-only and
# every shell command came back "rejected: blocked by policy", so two runs did
# nothing at all. The only way to measure the agent on Windows is
# `--dangerously-bypass-approvals-and-sandbox`, which is what `none` means
# here. It is never chosen silently: run.py picks it only on Windows, says so
# before the first run, and every row records it in isolation facts.
UNSANDBOXED = "none"

# Files in CODEX_HOME that reach every run whatever the flags say: the
# user-level instructions Codex adds to every prompt, and a user-level hooks
# file, which could send a run's calls to the owner's own Threefold stack.
# `--ignore-user-config` names config.toml only, so the runner refuses to
# measure while any of these is present.
#
# This list is not everything that reaches a run, and `isolation_facts` says so
# with every row. Five of the 2026-09-23 runs printed a notice of their own
# that skills were loaded and their descriptions shortened, under
# `--ignore-user-config`, so `CODEX_HOME/skills` and `CODEX_HOME/plugins`
# survive the flag. What they hold was not examined, so whether one of them
# could change an outcome is not known: they are context this runner does not
# control and does not look for.
USER_LEVEL_FILES = ("AGENTS.md", "AGENTS.override.md", "hooks.json")

# The items that are tool calls, and which of them the Threefold hook governs:
# `file_change` is apply_patch and `command_execution` the shell, the two the
# installer's matcher `apply_patch|Edit|Write|Bash` reaches. Both names were
# seen arriving at the hook, as `apply_patch` and `Bash`, in the 2026-09-23
# runs.
#
# These count what Codex printed, and Codex prints no item for a call the hook
# refused, so a count taken from the JSON alone is short by exactly the refused
# calls. Nothing here can fix that - the item is not in the file - so the hook
# log is read beside the JSON: `read_hook_log` and `merge_hook_log`, called by
# `harness.read_agent_transcript` for every Threefold run, put the hook's own
# calls and refusals back. A reader of `tool_uses` alone is reading Codex's
# account of itself.
TOOL_ITEMS = ("command_execution", "file_change", "mcp_tool_call", "web_search")
GOVERNED_ITEMS = frozenset({"command_execution", "file_change"})


def exec_flags_problem(help_text: str) -> Optional[str]:
    """Why this Codex cannot take the command below, or None when every flag it passes is listed."""
    if "--json" not in help_text and "exec" not in help_text:
        return "`codex exec --help` printed nothing the runner recognises"
    missing = [flag for flag in REQUIRED_EXEC_FLAGS if not re.search(re.escape(flag) + r"(?![\w-])", help_text)]
    if missing:
        return (f"this Codex's `exec` does not list {', '.join(missing)}; the runner passes them and was written "
                "against codex-cli 0.155.0")
    return None


def toml_literal(text: str) -> str:
    """A TOML literal string: no escapes inside, so a Windows path survives as it is."""
    if "'" in text or "\n" in text or "\r" in text:
        raise ValueError("a path holding a quote or a line break cannot be written as a TOML literal string")
    return f"'{text}'"


def build_command(codex: str, repo: Path, model: Optional[str], sandbox: str = DEFAULT_SANDBOX) -> List[str]:
    """The headless Codex command for one run. The prompt goes on stdin, named by the final `-`."""
    if sandbox not in SANDBOXES:
        raise ValueError(f"sandbox must be one of {', '.join(SANDBOXES)}")
    trusted = "projects={" + toml_literal(str(repo)) + "={trust_level='trusted'}}"
    command = [
        codex, "exec", "--json", "--color", "never",
        "--ephemeral", "--ignore-user-config", "--ignore-rules",
    ]
    if sandbox == UNSANDBOXED:
        # Approvals go with the sandbox in this flag: there is nothing left to
        # approve against. The run is confined by the work root and the task
        # repository alone, which the isolation facts say out loud.
        command += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        command += ["--sandbox", sandbox, "--config", "approval_policy='never'"]
    command += [
        "--enable", "hooks", "--dangerously-bypass-hook-trust", "--config", trusted,
        "--cd", str(repo),
    ]
    if model:
        command += ["--model", model]
    return command + ["-"]


def user_level_files(codex_home: Path) -> List[Path]:
    """The files in CODEX_HOME that would reach every run (see USER_LEVEL_FILES)."""
    return [Path(codex_home) / name for name in USER_LEVEL_FILES if (Path(codex_home) / name).exists()]


def _item_text(item: Mapping[str, Any]) -> str:
    parts: List[str] = []
    for key in ("text", "aggregated_output", "message", "output"):
        value = item.get(key)
        if isinstance(value, str):
            parts.append(value)
    error = item.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        parts.append(error["message"])
    elif isinstance(error, str):
        parts.append(error)
    return "\n".join(parts)


def parse_events(path: Path, classify: Callable[[str], str] = lambda text: "OTHER") -> Dict[str, Any]:
    """What a Codex run did, from `codex exec --json`, in the shape harness.parse_transcript returns.

    Codex prints no result message and no turn count, so both are made here:
    the result is a success when the last turn event is `turn.completed` and
    an error carrying Codex's own message when it is `turn.failed` or when only
    `error` events came; `num_turns` counts the agent's tool calls and
    messages. Cost is not reported by Codex and stays None. A refusal is any
    item whose text carries the hook's refusal marker; a command Codex itself
    declined without it is recorded as a permission denial.

    What the 2026-09-23 runs change about that. `turn.started` arrives and is
    ignored, which is right. `turn.failed` and a top-level `error` event never
    arrived, so the error branch is unexercised; the `error`s that did arrive
    are items inside an `item.completed`, and they are warnings as often as
    failures, which is why an error item is read for the refusal marker and
    never on its own taken for a failed run. `status: "declined"` never arrived
    either, so `permission_denials` was empty in every real run: the calls
    Codex did not make after a refusal it simply did not make, and said so in
    its message. And the call the hook refused left no item at all, so
    `tool_uses` here is short by it; `merge_hook_log` is what puts the hook's
    own count back.
    """
    summary: Dict[str, Any] = {
        "init": {}, "result": None, "tool_uses": Counter(), "refusals": [], "hook_events": Counter(), "lines": 0,
        "assistant_messages": 0, "hook_unjudged": 0, "hook_errors": 0,
    }
    seen_tools: Dict[str, str] = {}
    refused_items = set()
    denials: List[Dict[str, Any]] = []
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
    last_turn: Optional[str] = None
    failure = ""
    errors: List[str] = []
    last_message = ""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        summary["lines"] += 1
        kind = message.get("type")
        if kind == "thread.started":
            summary["init"] = {"thread_id": message.get("thread_id")}
        elif kind == "turn.completed":
            last_turn = "completed"
            for name in usage:
                usage[name] += int((message.get("usage") or {}).get(name) or 0)
        elif kind == "turn.failed":
            last_turn = "failed"
            error = message.get("error")
            failure = str(error.get("message") if isinstance(error, dict) else error or "turn failed")
        elif kind == "error":
            errors.append(str(message.get("message") or "error"))
        elif kind in ("item.started", "item.updated", "item.completed"):
            item = message.get("item") if isinstance(message.get("item"), dict) else {}
            item_type = str(item.get("type") or item.get("item_type") or "")
            item_id = str(item.get("id") or f"line-{summary['lines']}")
            if item_type in TOOL_ITEMS and item_id not in seen_tools:
                seen_tools[item_id] = item_type
                summary["tool_uses"][item_type] += 1
            if kind != "item.completed":
                continue
            if item_type == "agent_message":
                summary["assistant_messages"] += 1
                last_message = str(item.get("text") or "")
            text = _item_text(item)
            if REFUSAL_MARKER in text and item_id not in refused_items:
                refused_items.add(item_id)
                summary["refusals"].append({"tool": item_type or "?", "kind": classify(text)})
            elif item.get("status") == "declined" and item_type in TOOL_ITEMS:
                target = item.get("command") or ", ".join(
                    str(change.get("path")) for change in item.get("changes") or [] if isinstance(change, dict))
                denials.append({"tool_name": item_type, "tool_input": {"command": str(target)}})

    tool_calls = sum(summary["tool_uses"].values())
    result: Optional[Dict[str, Any]] = None
    if last_turn == "completed":
        result = {"subtype": "success", "is_error": False, "terminal_reason": "completed", "result": last_message}
    elif last_turn == "failed" or errors:
        result = {"subtype": "error_during_execution", "is_error": True, "terminal_reason": "",
                  "result": failure or errors[-1]}
    if result is not None:
        result.update({
            "num_turns": tool_calls + summary["assistant_messages"],
            "usage": {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cache_read_input_tokens": usage["cached_input_tokens"],
                "cache_creation_input_tokens": None,
                "reasoning_output_tokens": usage["reasoning_output_tokens"],
            },
            "total_cost_usd": None,
            "duration_ms": None,
            "permission_denials": denials,
        })
    summary["result"] = result
    return summary


def read_hook_log(path: Path) -> Dict[str, Any]:
    """What the per-run hook wrapper recorded for each call: how often the hook ran, refused a call (and by which
    gate), let a call through unjudged, or crashed.

    Codex prints no hook events in its JSON, so this log, written by the
    wrapper Codex runs as its hook, is the evidence that the hook started. It
    is also the evidence of a refusal, and the 2026-09-23 runs say why it has
    to be: Codex's JSON does not quote the hook's reason and prints no item at
    all for the refused call, which appears only here and in `stderr`. A
    credential refused on the machine never reaches the server's ledger either.
    """
    counts: Dict[str, Any] = {"calls": 0, "unjudged": 0, "crashed": 0, "refused": 0, "refused_by_kind": Counter()}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return counts
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        counts["calls"] += 1
        counts["unjudged"] += 1 if record.get("unjudged") else 0
        counts["crashed"] += 1 if record.get("crashed") or record.get("exit") not in (0, None) else 0
        if record.get("refused"):
            counts["refused"] += 1
            counts["refused_by_kind"][str(record.get("kind") or "OTHER")] += 1
    return counts


def merge_hook_log(summary: Dict[str, Any], counts: Mapping[str, Any]) -> Dict[str, Any]:
    """The hook log's facts in the transcript summary: its calls as hook events, and the refusals the JSON did not show.

    Refusals are topped up, never added: when Codex's JSON does quote the
    hook, the transcript already holds the same refusals, so only as many as
    the log counts beyond them are added, named by the gates the log has more
    of. The total is then the larger of the two counts, never their sum.
    """
    if counts.get("calls"):
        summary["hook_events"] = Counter({"hook_response": int(counts["calls"])})
    summary["hook_unjudged"] = int(counts.get("unjudged") or 0)
    summary["hook_errors"] = int(counts.get("crashed") or 0)
    refusals = summary.setdefault("refusals", [])
    missing = int(counts.get("refused") or 0) - len(refusals)
    if missing > 0:
        seen = Counter(str(item.get("kind")) for item in refusals)
        for kind, count in sorted((counts.get("refused_by_kind") or {}).items()):
            for _ in range(max(0, int(count) - seen.get(kind, 0))):
                if missing <= 0:
                    break
                refusals.append({"tool": "?", "kind": kind})
                missing -= 1
        refusals.extend({"tool": "?", "kind": "OTHER"} for _ in range(missing))
    return summary


def version_of(codex: str, runner: Callable[..., Any]) -> str:
    try:
        completed = runner([codex, "--version"], capture_output=True, timeout=60)
        return completed.stdout.decode("utf-8", "replace").strip()[:80]
    except Exception:  # noqa: BLE001 - a version string is a nicety, never a failure
        return "unavailable"


def exec_help(codex: str, runner: Callable[..., Any]) -> str:
    try:
        completed = runner([codex, "exec", "--help"], capture_output=True, timeout=60)
        return completed.stdout.decode("utf-8", "replace") + completed.stderr.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - no help text means the flag check refuses, which is the safe side
        return ""


def isolation_facts(sandbox: str = DEFAULT_SANDBOX) -> Dict[str, Any]:
    """What a Codex run's set-up does and does not keep out, recorded with every row.

    `skills_and_plugins` is the fact the first real runs added: the checks
    above are by file name, and the names they know are not all that reaches a
    run. A row that claims isolation has to carry what the isolation missed.
    """
    if sandbox == UNSANDBOXED:
        return {
            "mode": "codex-user-login",
            "config_dir": "the owner's CODEX_HOME, for the login only (--ignore-user-config, --ignore-rules, --ephemeral)",
            "home": "the owner's",
            "user_settings_and_hooks": "config.toml and execpolicy rules skipped; the runner refuses to start while "
                                       "CODEX_HOME holds AGENTS.md, AGENTS.override.md or hooks.json",
            "skills_and_plugins": "not kept out: runs on 2026-09-23 printed their own notice that skills were loaded "
                                  "under --ignore-user-config, so CODEX_HOME/skills and CODEX_HOME/plugins reach a run "
                                  "and the runner does not look for them",
            "hook_trust": "--dangerously-bypass-hook-trust, and the repository trusted for this invocation only",
            "sandbox": "NONE: --dangerously-bypass-approvals-and-sandbox, because Codex does not sandbox on Windows "
                       "and refuses every command under --sandbox there. The run is confined by the work root and "
                       "the task repository only, and nothing stops it reaching the rest of the machine",
            "reads": "anything the owner's account can read",
            "edits": "anything the owner's account can write; only the task repository is measured",
            "shell": "unsandboxed, with no approval prompt",
            "installs": "no package index for pip, uv or npm, and pip requires a virtual environment",
        }
    return {
        "mode": "codex-user-login",
        "config_dir": "the owner's CODEX_HOME, for the login only (--ignore-user-config, --ignore-rules, --ephemeral)",
        "home": "the owner's",
        "user_settings_and_hooks": "config.toml and execpolicy rules skipped; the runner refuses to start while "
                                   "CODEX_HOME holds AGENTS.md, AGENTS.override.md or hooks.json",
        "skills_and_plugins": "not kept out: runs on 2026-09-23 printed their own notice that skills were loaded "
                              "under --ignore-user-config, so CODEX_HOME/skills and CODEX_HOME/plugins reach a run "
                              "and the runner does not look for them",
        "hook_trust": "--dangerously-bypass-hook-trust, and the repository trusted for this invocation only",
        "sandbox": "--sandbox as recorded in harness.codex_sandbox, approval_policy='never'",
        "reads": "whatever the Codex sandbox allows; the owner's private folders are not denied by name",
        "edits": "the repository and Codex's own temporary folders under workspace-write",
        "shell": "Codex's sandbox; no prefix rules as for Claude Code",
        "installs": "no package index for pip, uv or npm, and pip requires a virtual environment",
    }

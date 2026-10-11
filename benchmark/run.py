"""Runs the benchmark: real coding-agent sessions on the Acme tasks, under each condition, recorded as JSON lines.

    python benchmark/run.py --check-auth                                # does the login work? (one tiny call)
    python benchmark/run.py --tasks orders-s3-archive --reps 1 --pilot
    python benchmark/run.py --reps 3 --parallel 3                       # the full matrix, 54 runs
    python benchmark/run.py --family pressure --reps 3 --parallel 3     # the pressure tasks, 27 runs
    python benchmark/run.py --reps 3 --parallel 3 --resume <run-id>     # carry on where it stopped
    python benchmark/run.py --agent codex --reps 3 --parallel 3         # the same matrix with Codex

Without --tasks the matrix is the standard family's six tasks, whose prompts
tempt a violation. The pressure family's three tasks, whose prompts ask for the
forbidden shortcut, run only with --family pressure (or all, or their ids in
--tasks); a run of them alone is named <time>-pressure.

Each run gets a fresh copy of the task repository under the system temp folder
(%TEMP%\\threefold-bench\\<run-id> on Windows), never inside the workspace, or
at the root of the same drive when a Claude memory file sits above the temp
folder, because Claude Code would load it into every run. One row per run is
appended to benchmark/results/<run-id>.jsonl as soon as the run ends, so an
interrupted matrix keeps what it finished. Aggregate with benchmark/report.py.

The login (credentials.py). Claude Code logs in with a token file when there
is one: the owner runs `claude setup-token` once and saves the token, alone on
one line, to C:\\threefold-bench\\.claude-oauth-token (or passes --token-file).
The runner reads it and hands it to the agent process alone, with a
configuration folder and a home folder of the run's own, so nothing from the
owner's settings, hooks, skills or memory reaches the agent. Without a token
file it uses the machine's login. `--check-auth` makes one trivial call the
way a run would and says ok, expired, missing, limited or error, with the next
step.

A run the service stops (a usage limit or an overload) is recorded as cut
short, and after a pause (--retry-pause, 180 s) it is tried once more in a
fresh folder; the row records both attempts. If the second attempt is stopped
too, or the login stops working, no further run is started: the runner prints
the command that resumes the matrix and exits with 3. `--resume <run-id>`
appends to the same results file and runs only what the report would not yet
count: a planned run whose latest row the report counts is skipped, and one
that was cut short, never reached the model, hit a harness error, or was a
Threefold run without a working Threefold in front of it (the local server
stopped answering, the hook failed open or never fired) is run again. The
report counts only the latest row of each run.

`--threefold-endpoint URL` points the Threefold conditions at a Threefold that
is already running (https, or http to this machine for a stand-in) instead of
a local server per run: the hook names the project Acme-Live-<task> and the
session live-<task>-<date>, and the run's decisions are read back from that
Threefold's GET /api/decisions. Every row records which (`ledger_source`,
`threefold_endpoint`). scripts/daily_live_agent.py runs one such run a day.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark import codex_agent, credentials, harness, report, task_library  # noqa: E402

RESULTS_DIR = harness.BENCHMARK_DIR / "results"
DEFAULT_RETRY_PAUSE_S = 180.0
STOPPED_EXIT_CODE = 3


def _csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Threefold agent benchmark.")
    parser.add_argument("--agent", choices=("claude-code", "claude", "codex", "scripted"), default="claude-code",
                        help="claude-code (default; `claude` is the older spelling), codex, or scripted, which tests the "
                             "harness with a fixed script instead of a model and whose rows measure nothing")
    parser.add_argument("--tasks", type=_csv, default=None,
                        help="comma-separated task ids, of either family (default: every task of --family)")
    parser.add_argument("--family", choices=task_library.FAMILIES + ("all",), default=None,
                        help="standard (the default without --tasks: the six tasks whose prompts tempt), pressure (the "
                             "three whose prompts ask for the forbidden shortcut), or all; with --tasks, every task named "
                             "must be in it")
    parser.add_argument("--conditions", type=_csv, default=list(harness.DEFAULT_CONDITIONS),
                        help="comma-separated: none, prompt, threefold, prompt+threefold")
    parser.add_argument("--reps", type=int, default=3, help="repetitions of each task and condition (default 3)")
    parser.add_argument("--model", default=None,
                        help=f"the agent's model (default {harness.DEFAULT_MODEL} for Claude Code; for Codex its own "
                             "default, recorded as codex-default: pass one to pin it)")
    parser.add_argument("--parallel", type=int, default=1, help="runs at once (default 1)")
    parser.add_argument("--max-turns", type=int, default=harness.DEFAULT_MAX_TURNS, help="Claude Code only")
    parser.add_argument("--timeout", type=int, default=harness.DEFAULT_TIMEOUT_S, help="seconds per run before it is stopped")
    parser.add_argument("--budget-usd", type=float, default=harness.DEFAULT_BUDGET_USD,
                        help="claude --max-budget-usd per run (Claude Code only)")
    parser.add_argument("--isolation", choices=("auto", "fresh-config", "user-config"), default="auto",
                        help="Claude Code: fresh-config (a token file, a configuration folder per run) or user-config "
                             "(the machine's login); auto picks fresh-config when a token file exists")
    parser.add_argument("--token-file", type=Path, default=None,
                        help="a file holding a token from `claude setup-token`, alone on one line (default: "
                             f"{credentials.DEFAULT_TOKEN_FILE} when that file exists)")
    parser.add_argument("--check-auth", action="store_true",
                        help="make one trivial headless call with the login a run would use, report ok, expired, "
                             "missing, limited or error with the next step, and exit")
    parser.add_argument("--claude", default=None, help="path to the claude executable (default: found on PATH)")
    parser.add_argument("--codex", default=None, help="path to the codex executable (default: found on PATH)")
    parser.add_argument("--codex-sandbox", choices=codex_agent.SANDBOXES, default=None,
                        help="Codex's sandbox for shell commands (default workspace-write)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--resume", metavar="RUN_ID", default=None,
                        help="append to results/<RUN_ID>.jsonl and run only the planned runs the report does not count "
                             "yet (cut short, not run, harness errors and Threefold runs without a working Threefold are "
                             "run again)")
    parser.add_argument("--retry-pause", type=float, default=DEFAULT_RETRY_PAUSE_S,
                        help="seconds to wait before trying once more a run the service cut short (default 180)")
    parser.add_argument("--pilot", action="store_true", help="label every row as a pilot, not a result")
    parser.add_argument("--work-root", type=Path, default=None,
                        help="default: <system temp>/threefold-bench/<run-id>, or <drive root>/threefold-bench/<run-id> "
                             "when a CLAUDE.md sits above the temp folder")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--threefold-endpoint", metavar="URL", default=None,
                        help="the Threefold conditions report to this Threefold instead of a local server started for "
                             "each run: https, or http to this machine only for a stand-in. The hook names the project "
                             "Acme-Live-<task> and the session live-<task>-<date>, and the run's ledger is read back "
                             "from GET /api/decisions there")
    parser.add_argument("--live-date", metavar="YYYY-MM-DD", default=None,
                        help="the UTC day in a remote run's session names (default: today, UTC); only with "
                             "--threefold-endpoint")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and the agent command, run nothing")
    args = parser.parse_args(argv)
    args.agent = harness.normalise_agent(args.agent)
    if args.live_date and not args.threefold_endpoint:
        parser.error("--live-date names a remote run's sessions; give --threefold-endpoint with it")
    if args.threefold_endpoint:
        try:
            args.remote = harness.RemoteThreefold(args.threefold_endpoint, args.live_date or utc_day())
        except ValueError as error:
            parser.error(f"--threefold-endpoint / --live-date: {error}")
    else:
        args.remote = None
    unknown = [condition for condition in args.conditions if condition not in harness.CONDITIONS]
    if unknown:
        parser.error(f"unknown condition(s) {', '.join(unknown)}; choose from {', '.join(harness.CONDITIONS)}")
    if args.reps < 1 or args.parallel < 1:
        parser.error("--reps and --parallel must be at least 1")
    if args.retry_pause < 0:
        parser.error("--retry-pause cannot be negative")
    if args.token_file is not None and args.agent != "claude-code":
        parser.error("--token-file is for Claude Code; Codex keeps its login in CODEX_HOME (`codex login`)")
    if args.resume and args.run_id and args.run_id != args.resume:
        parser.error("--resume names the run id; leave out --run-id or give the same one")
    if args.model is None and args.agent == "claude-code":
        args.model = harness.DEFAULT_MODEL
    return args


def utc_day(now: Optional[datetime.datetime] = None) -> str:
    """Today in UTC, YYYY-MM-DD: the day a remote run's sessions are named after."""
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%d")


def remote_problem(rows: Sequence[Mapping[str, Any]], remote: Optional[harness.RemoteThreefold],
                   tasks: Sequence[task_library.Task]) -> Optional[str]:
    """Why a matrix must not run against this Threefold, or None.

    Every task's live project must be a name the stack shows, and a run id's
    Threefold rows all come from one place: the local server of each run, or
    one remote endpoint. A resume that switched would mix two ledgers in one
    results file.
    """
    if remote is not None:
        for task in tasks:
            try:
                remote.project(task)
            except ValueError as error:
                return str(error)
    wanted = remote.endpoint if remote is not None else None
    recorded = {row.get("threefold_endpoint") for row in rows if harness.uses_threefold(str(row.get("condition") or ""))}
    if recorded and recorded != {wanted}:
        where = ", ".join(sorted(str(value or "a local server") for value in recorded))
        return (f"the recorded Threefold rows reported to {where}, and this run would report to "
                f"{wanted or 'a local server'}; pass the same --threefold-endpoint as before")
    return None


def default_run_id(pilot: bool, agent: str, now: Optional[datetime.datetime] = None,
                   families: Iterable[str] = ()) -> str:
    """The run's id, which names its results file: a matrix of the pressure tasks alone says so."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    agent = harness.normalise_agent(agent)
    suffix = ("-pressure" if set(families) == {"pressure"} else "") + ("-pilot" if pilot else "")
    suffix += ("-scripted" if agent == "scripted" else "") + ("-codex" if agent == "codex" else "")
    return now.strftime("%Y%m%dT%H%M%SZ") + suffix


def select_tasks(names: Optional[Sequence[str]], family: Optional[str]) -> List[task_library.Task]:
    """The tasks a matrix runs. Named tasks run whatever their family, unless --family names another; without names,
    every task of --family, and the standard family when it is not given, so the documented full matrix stays the
    standard tasks' and the pressure tasks run only when asked for."""
    wanted = None if family == "all" else family
    if names:
        return task_library.load_tasks(names, family=wanted)
    return task_library.load_tasks(family="standard" if family is None else wanted)


def plan_runs(tasks: Sequence[task_library.Task], conditions: Sequence[str], reps: int) -> List[tuple]:
    """Every run, interleaved so that the conditions of one task and repetition sit next to each other.

    With --parallel equal to the number of conditions they then run side by
    side, at the same time of day against the same service, and a matrix cut
    short is still balanced across conditions.
    """
    return [(task, condition, rep) for rep in range(1, reps + 1) for task in tasks for condition in conditions]


def _version(executable: str) -> str:
    try:
        completed = subprocess.run([executable, "--version"], capture_output=True, timeout=60)
        return completed.stdout.decode("utf-8", "replace").strip()[:80]
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def claude_version(claude: str) -> str:
    return _version(claude)


def claude_help(claude: str) -> str:
    try:
        completed = subprocess.run([claude, "--help"], capture_output=True, timeout=60)
        return completed.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def setting_sources_problem(help_text: str) -> Optional[str]:
    """Why this Claude Code cannot take `--setting-sources project,local`, or None when it can.

    The runner leans on that flag to keep the owner's user settings, hooks and
    user-level CLAUDE.md out. Claude Code 2.1.220 lists its sources as `user,
    project, local`; newer documentation names different ones (user,
    workspace, machine, managed, sdk). A version that no longer knows
    `project` and `local` would ignore or reject the flag, so the runner stops
    before any run instead of measuring an agent that read the owner's setup.
    """
    start = help_text.find("--setting-sources")
    if start == -1:
        return "this Claude Code has no --setting-sources option"
    following = help_text[start + len("--setting-sources"):]
    next_option = re.search(r"\n\s+--?[A-Za-z]", following)
    description = following[: next_option.start()] if next_option else following
    words = set(re.findall(r"[a-z]+", description.lower()))
    missing = [name for name in ("project", "local") if name not in words]
    if missing:
        return (f"this Claude Code's --setting-sources does not list {', '.join(missing)}; the runner passes "
                "`--setting-sources project,local` and was checked against 2.1.220")
    return None


def default_work_root(run_id: str, temp: Optional[Path] = None) -> Path:
    """<system temp>/threefold-bench/<run-id>, unless a Claude memory file sits above it.

    Claude Code loads CLAUDE.md files from every folder above its working
    directory. On Windows the system temp folder lies inside the home folder,
    so when the owner keeps a ~/.claude/CLAUDE.md, every run below the temp
    folder would read it as project instructions. The fallback is the same
    folder name at the root of the temp folder's drive.
    """
    temp = Path(temp or tempfile.gettempdir())
    candidates = [temp / "threefold-bench" / run_id, Path(temp.anchor or "/") / "threefold-bench" / run_id]
    for candidate in candidates:
        if not harness.claude_memory_above(candidate):
            return candidate
    return candidates[0]


def codex_home(env: Optional[Mapping[str, str]] = None) -> Path:
    """Where Codex keeps its login: CODEX_HOME when set, ~/.codex otherwise. Only its path is used here."""
    env = os.environ if env is None else env
    configured = (env.get("CODEX_HOME") or "").strip()
    return Path(os.path.expanduser(configured)) if configured else Path.home() / ".codex"


class ResultsFile:
    """Appends one JSON line per run, from any thread."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, row: Dict[str, Any]) -> None:
        line = json.dumps(row, sort_keys=True, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")

    def rows(self) -> List[Dict[str, Any]]:
        if not self.path.is_file():
            return []
        rows = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    raise ValueError(f"{self.path}:{number} is not JSON") from None
        return rows


# --- resuming ------------------------------------------------------------------------

RunKey = Tuple[str, str, str, int]


def run_key(row: Mapping[str, Any]) -> RunKey:
    """What identifies a planned run across attempts and resumes: agent, task, condition, repetition."""
    return (harness.normalise_agent(str(row.get("agent") or "claude-code")), str(row.get("task")),
            str(row.get("condition")), int(row.get("rep") or 0))


def measured_row(row: Mapping[str, Any]) -> bool:
    """A row the report counts: its run ended on its own course, the harness did its part, and a Threefold run had
    a working Threefold in front of it.

    This is the report's own judgement (report.is_valid), so a resume and the
    report agree on what is done: a Threefold run whose local server stopped
    answering, or whose hook failed open, measured nothing the report can
    use, and a resume runs it again instead of leaving the matrix short.
    """
    return report.is_valid(row)


def done_keys(rows: Iterable[Mapping[str, Any]]) -> Set[RunKey]:
    """The planned runs a resume skips: those whose latest row the report counts."""
    latest: Dict[RunKey, Mapping[str, Any]] = {}
    for row in rows:
        latest[run_key(row)] = row
    return {key for key, row in latest.items() if measured_row(row)}


def attempts_so_far(rows: Iterable[Mapping[str, Any]]) -> Counter:
    """How many attempts each planned run has had, first attempts and retries alike."""
    counts: Counter = Counter()
    for row in rows:
        counts[run_key(row)] += int(row.get("attempts") or 1)
    return counts


def _recorded(row: Mapping[str, Any], field: str) -> Optional[str]:
    """What a row recorded about its login (`auth`) or its isolation mode, or None when it predates the field."""
    value = row.get("auth") if field == "auth" else (row.get("isolation") or {}).get("mode")
    return str(value) if value else None


def resume_problem(rows: Sequence[Mapping[str, Any]], agent: str, model: Optional[str], pilot: bool,
                   auth: Optional[str] = None, isolation: Optional[str] = None,
                   families: Optional[Iterable[str]] = None) -> Optional[str]:
    """Why these arguments must not add to these rows, or None.

    One run id holds one agent's rows, with one model, one login (`auth`),
    one isolation mode and one pilot label. The printed resume command keeps
    every argument, so this catches the command typed again by hand: without
    `--agent codex` a Codex run id would silently gain a Claude Code matrix,
    and a resume without the token file would mix machine-login rows into a
    token-file run. `isolation` is the mode a row records (isolation_facts).
    `families` are the task families the resume would run: a pressure matrix
    resumed without `--family pressure` would otherwise gain the standard one.
    """
    if not rows:
        return None
    agents = sorted({run_key(row)[0] for row in rows})
    if agent not in agents:
        return (f"the recorded rows are {', '.join(agents)} rows, and this resume would add {agent} rows; "
                f"pass --agent {agents[0]}")
    recorded_families = {report.family_of(row) for row in rows}
    planned_families = set(families or ())
    if planned_families and not planned_families & recorded_families:
        recorded = ", ".join(name for name in task_library.FAMILIES if name in recorded_families)
        planned = ", ".join(name for name in task_library.FAMILIES if name in planned_families)
        return (f"the recorded rows are of the {recorded} task family, and this resume would run the {planned} family; "
                f"pass --family {recorded} or the same --tasks as before")
    same_agent = [row for row in rows if run_key(row)[0] == agent]
    for field, wanted_value, advice in (("auth", auth, "use the same token file, or none, as before"),
                                        ("isolation", isolation, "pass the same --isolation as before")):
        recorded = {value for value in (_recorded(row, field) for row in same_agent) if value}
        if wanted_value is not None and recorded and recorded != {wanted_value}:
            return (f"the recorded {agent} rows used {field} {', '.join(sorted(recorded))}, and this resume would use "
                    f"{wanted_value}; {advice}")
    models = {str(row.get("model")) for row in same_agent}
    wanted = harness.recorded_model(harness.AgentOptions(agent=agent, model=model))
    if same_agent and models != {wanted}:
        return (f"the recorded {agent} rows used model {', '.join(sorted(models))}, and this resume would use {wanted}; "
                "pass the same --model")
    pilots = {bool(row.get("pilot")) for row in rows}
    if pilots != {pilot}:
        return ("the recorded rows are " + ("a pilot" if True in pilots else "not a pilot")
                + ", and this resume is " + ("a pilot" if pilot else "not a pilot") + "; pass --pilot the same way")
    return None


def resume_command(argv: Sequence[str], run_id: str) -> str:
    """The command that carries on this matrix: the same arguments, with --resume <run-id>."""
    kept: List[str] = []
    skip = False
    for item in argv:
        if skip:
            skip = False
            continue
        if item in ("--resume", "--run-id"):
            skip = True
            continue
        if item.startswith(("--resume=", "--run-id=")):
            continue
        kept.append(f'"{item}"' if " " in item else item)
    return " ".join(["python", "benchmark/run.py", *kept, "--resume", run_id])


# --- one planned run, tried at most twice ------------------------------------------------

def free_attempt(plan: harness.RunPlan, task: task_library.Task, condition: str, rep: int, start: int) -> int:
    """The first attempt number at or after start whose folder does not exist yet."""
    attempt = max(1, start)
    while harness.run_dir_for(plan, task, condition, rep, attempt).exists():
        attempt += 1
    return attempt


def run_with_retry(task: task_library.Task, condition: str, rep: int, plan: harness.RunPlan, pause_s: float,
                   stop: threading.Event, first_attempt: int = 1,
                   base_env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """One planned run, and one more attempt after a pause when the service cut the first one short.

    Only a usage limit or an overload is tried again: a login that stopped
    working does not recover by waiting. The row returned is the last
    attempt's, with `attempts` and the first attempt's ending in
    `first_attempt`, so a planned run is one row and never counted twice.
    If the matrix is stopping, the retry is not made and the first row stands.
    """
    attempt = free_attempt(plan, task, condition, rep, first_attempt)
    row = harness.run_one(task, condition, rep, plan, base_env, attempt=attempt)
    row["attempts"] = 1
    row["first_attempt"] = None
    if measured_row(row) or row.get("service_failure") not in harness.RETRYABLE_FAILURES:
        return row
    first = {key: row.get(key) for key in ("attempt", "run_end", "service_failure", "agent_error", "run_dir", "started_at")}
    if stop.wait(pause_s) or stop.is_set():
        row["retry_skipped"] = "the matrix was stopping"
        return row
    second = harness.run_one(task, condition, rep, plan, base_env, attempt=free_attempt(plan, task, condition, rep, attempt + 1))
    second["attempts"] = 2
    second["first_attempt"] = first
    return second


def stop_reason(row: Mapping[str, Any]) -> Optional[str]:
    """Why no further run should start after this row, or None."""
    if measured_row(row):
        return None
    kind = row.get("service_failure")
    if kind == "auth":
        return f"the login stopped working ({row.get('agent_error') or 'refused'}); run --check-auth"
    if kind in harness.RETRYABLE_FAILURES and int(row.get("attempts") or 1) >= 2:
        return f"the service still refused after a pause ({kind}: {row.get('agent_error') or 'no message'})"
    return None


def one_line(row: Dict[str, Any]) -> str:
    verdict = "VIOLATION" if row.get("violation_landed") else "clean"
    tests = "tests pass" if row.get("acceptance_passed") else "tests fail"
    extra = f", {row.get('hook_refusals') or 0} refusal(s)" if harness.uses_threefold(row["condition"]) else ""
    problem = row.get("harness_error") or ("" if row.get("agent_ran") else f"agent did not run: {row.get('agent_error')}")
    if not problem and str(row.get("run_end") or "").startswith("cut_short"):
        problem = f"{row['run_end']}: {row.get('agent_error')}"
    if not problem and row.get("measured") and not measured_row(row):
        problem = f"not counted: {report.invalid_reason(row)}"
    retried = f" (attempt {row.get('attempt')}, retried)" if int(row.get("attempts") or 1) > 1 else ""
    return (f"{row['task']:<28} {row['condition']:<17} r{row['rep']}  {verdict:<9} {tests}{extra}"
            f"  turns={row.get('num_turns')} cost={row.get('cost_usd')}{retried}" + (f"  [{problem}]" if problem else ""))


# --- the login check ---------------------------------------------------------------------

def _token_or_error(args: argparse.Namespace) -> Tuple[Optional[credentials.Credential], Optional[Path], Optional[str]]:
    """The token file's credential (or None for the machine's login), the path shown to the owner, and any problem."""
    path = credentials.resolve_token_file(args.token_file)
    shown = path or credentials.DEFAULT_TOKEN_FILE
    if path is None:
        return None, shown, None
    try:
        return credentials.read_token_file(path), shown, None
    except credentials.TokenFileError as error:
        return None, shown, str(error)


def check_auth_main(args: argparse.Namespace) -> int:
    if args.agent == "scripted":
        print("check-auth: the scripted agent needs no login.")
        return 0
    base = args.work_root.parent if args.work_root else default_work_root("check-auth").parent
    if args.agent == "codex":
        codex = args.codex or shutil.which("codex") or "codex"
        result = credentials.check_codex_auth(codex, codex_home(), work_base=base)
    else:
        credential, shown, problem = _token_or_error(args)
        if not problem:
            # The login the matrix would use: with --isolation user-config a token file is left unused, so the
            # check must not log in with it and report a login the runs never try.
            try:
                if harness.choose_isolation(args.isolation, credential is not None) != "fresh-config":
                    credential = None
            except ValueError as error:
                problem = str(error)
        if problem:
            result = credentials.AuthCheck("missing", "token-file", problem, credentials.token_steps(shown))
        else:
            _warn_inherited_token()
            claude = args.claude or shutil.which("claude") or "claude"
            result = credentials.check_claude_auth(claude, credential, shown, args.model, work_base=base)
    for line in result.lines():
        print(line)
    return 0 if result.status == "ok" else 1


def _warn_inherited_token() -> None:
    if os.environ.get(credentials.TOKEN_ENV):
        print(f"note: {credentials.TOKEN_ENV} is set in this shell and is not used: runs log in with a token file "
              f"(--token-file, default {credentials.DEFAULT_TOKEN_FILE}) or the machine's login.", file=sys.stderr)


# --- the matrix --------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    if args.check_auth:
        return check_auth_main(args)
    try:
        tasks = select_tasks(args.tasks, args.family)
    except ValueError as error:
        print(f"refused: {error}.", file=sys.stderr)
        return 2
    task_families = sorted({task.family for task in tasks})
    run_id = args.resume or args.run_id or default_run_id(args.pilot, args.agent, families=task_families)
    results = ResultsFile(Path(args.results_dir) / f"{run_id}.jsonl")
    previous: List[Dict[str, Any]] = []
    if args.resume:
        if not results.path.is_file():
            print(f"refused: there is no results file for run {run_id} at {results.path}", file=sys.stderr)
            return 2
        previous = results.rows()
        problem = resume_problem(previous, args.agent, args.model, args.pilot, families=task_families)
        if problem:
            print(f"refused: {problem}.", file=sys.stderr)
            return 2
    problem = remote_problem(previous, args.remote, tasks)
    if problem:
        print(f"refused: {problem}.", file=sys.stderr)
        return 2
    work_root = args.work_root or default_work_root(run_id)
    harness.ensure_outside_workspace(work_root)

    claude = args.claude or shutil.which("claude") or "claude"
    codex = args.codex or shutil.which("codex") or "codex"
    credential: Optional[credentials.Credential] = None
    isolation = "user-config"
    if args.agent == "claude-code":
        credential, shown, problem = _token_or_error(args)
        if problem:
            print(f"refused: {problem}. Next step: {credentials.token_steps(shown)}", file=sys.stderr)
            return 2
        _warn_inherited_token()
        try:
            isolation = harness.choose_isolation(args.isolation, credential is not None)
        except ValueError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
        if isolation != "fresh-config":
            credential = None
        above = harness.claude_memory_above(work_root)
        if above:
            print("refused: Claude Code would load these memory files into every run as project instructions, because they sit "
                  f"above the work root {work_root}: {', '.join(str(path) for path in above)}. Pass --work-root at a folder "
                  "with no CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md or .claude/rules above it.", file=sys.stderr)
            return 2
        if not args.dry_run:
            problem = setting_sources_problem(claude_help(claude))
            if problem:
                print(f"refused: {problem}. Check `claude --help` and adjust harness.build_agent_command before measuring.",
                      file=sys.stderr)
                return 2
    elif args.agent == "codex" and not args.dry_run:
        home = codex_home()
        found = codex_agent.user_level_files(home)
        if found:
            print("refused: Codex would read these into every run whatever the flags say, because they sit in CODEX_HOME "
                  f"({home}): {', '.join(str(path) for path in found)}. Move them aside while the benchmark runs, or point "
                  "CODEX_HOME at a folder holding only a login (`codex login` with CODEX_HOME set to it).", file=sys.stderr)
            return 2
        problem = codex_agent.exec_flags_problem(codex_agent.exec_help(codex, subprocess.run))
        if problem:
            print(f"refused: {problem}. Check `codex exec --help` and adjust codex_agent.build_command before measuring.",
                  file=sys.stderr)
            return 2

    if previous:
        # Checked again now the login and the isolation are known, before anything runs.
        auth = "none" if args.agent == "scripted" else ("token-file" if credential is not None else "machine-login")
        mode = harness.isolation_facts(isolation, (), args.agent)["mode"]
        problem = resume_problem(previous, args.agent, args.model, args.pilot, auth=auth, isolation=mode,
                                 families=task_families)
        if problem:
            print(f"refused: {problem}.", file=sys.stderr)
            return 2

    # Codex does not sandbox on Windows: under --sandbox it tells the model the
    # workspace is read-only and rejects every command, so a run measures
    # nothing. There the default is no sandbox at all, and the run says so
    # before it starts rather than leaving it in a field nobody reads.
    if args.codex_sandbox is None:
        args.codex_sandbox = (codex_agent.UNSANDBOXED if (args.agent == "codex" and os.name == "nt")
                              else codex_agent.DEFAULT_SANDBOX)
    version = None
    if args.agent == "claude-code":
        version = claude_version(claude)
    elif args.agent == "codex":
        version = codex_agent.version_of(codex, subprocess.run)
    options = harness.AgentOptions(
        agent=args.agent, claude=claude, model=args.model, max_turns=args.max_turns, timeout_s=args.timeout,
        budget_usd=args.budget_usd, isolation=isolation, codex=codex, codex_sandbox=args.codex_sandbox,
        codex_home=codex_home() if args.agent == "codex" else None, agent_version=version,
    )
    plan = harness.RunPlan(run_id=run_id, work_root=work_root, options=options, pilot=args.pilot,
                           credential=credential, remote=args.remote)
    planned = plan_runs(tasks, args.conditions, args.reps)
    done = done_keys(previous)
    runs = [(task, condition, rep) for task, condition, rep in planned
            if (args.agent, task.id, condition, rep) not in done]
    prior = attempts_so_far(previous)

    print(f"run {run_id}: {len(planned)} run(s), {len(tasks)} task(s) x {len(args.conditions)} condition(s) x {args.reps} rep(s)"
          f"; task family: {', '.join(task_families)}")
    if args.resume:
        print(f"resuming: {len(planned) - len(runs)} already measured, {len(runs)} to run")
    agent_line = f"agent: {args.agent}"
    if args.agent != "scripted":
        agent_line += f" ({version}, model {harness.recorded_model(options)}, login {plan.auth}"
        agent_line += f", isolation {isolation})" if args.agent == "claude-code" else f", sandbox {args.codex_sandbox})"
    print(agent_line)
    if args.agent == "codex" and args.codex_sandbox == codex_agent.UNSANDBOXED:
        print("sandbox: NONE. Codex has no sandbox on this platform, so each run may read and write anything this "
              "account can. Only the task repository is measured, and every row records it.")
    if credential is not None:
        print(f"token file: {credential.path} (the token goes to the agent process only)")
    if any(harness.uses_threefold(condition) for condition in args.conditions):
        if args.remote is not None:
            print(f"threefold: the remote Threefold at {args.remote.endpoint}, projects Acme-Live-<task>, sessions "
                  f"live-<task>-{args.remote.date}; its ledger is read back from GET /api/decisions")
        else:
            print("threefold: a local server from this repository's source, started for each run")
    print(f"work root: {work_root}")
    print(f"results: {results.path}")
    if args.dry_run:
        sample_repo = Path("<run-dir>") / "repo"
        command = harness.build_agent_command(options, tasks[0], Path("<run-dir>") / "agent-settings.json",
                                              repo=sample_repo, private_files=[credential.path] if credential else [])
        print("command: " + " ".join(command) + "   (prompt on stdin)")
        for task, condition, rep in runs:
            print(f"  would run {task.id} / {condition} / r{rep}")
        return 0
    if not runs:
        print("nothing to run: every planned run has a measured row.")
        return 0

    work_root.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    reasons: List[str] = []
    lock = threading.Lock()

    def work(task: task_library.Task, condition: str, rep: int) -> Optional[Dict[str, Any]]:
        if stop.is_set():
            return None
        key = (args.agent, task.id, condition, rep)
        row = run_with_retry(task, condition, rep, plan, args.retry_pause, stop, prior[key] + 1)
        reason = stop_reason(row)
        if reason:
            with lock:
                reasons.append(reason)
            stop.set()
        return row

    finished = 0
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(work, task, condition, rep): (task, condition, rep) for task, condition, rep in runs}
        for future in as_completed(futures):
            row = future.result()
            if row is None:
                continue
            results.append(row)
            finished += 1
            print(f"[{finished}/{len(runs)}] {one_line(row)}", flush=True)
    if stop.is_set():
        print(f"stopped early: {reasons[0] if reasons else 'the matrix was stopped'}. {len(runs) - finished} run(s) were "
              "not started.", file=sys.stderr)
        print(f"resume with: {resume_command(argv, run_id)}", file=sys.stderr)
        return STOPPED_EXIT_CODE
    print(f"done. Aggregate with: python benchmark/report.py {results.path.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

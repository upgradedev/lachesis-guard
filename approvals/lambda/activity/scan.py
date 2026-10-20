"""The deterministic change scan of one commit (B7a).

This is a deterministic scan, and everything that shows its result says
so: it is a set of plain path and line rules, never a Decider score and
never a model call. A finding names the rule, the file path and a fixed
reason; it never quotes the line it matched, so a secret the scan finds
is not copied into the table, the bus or the API.

The rules, each a finding of its own:

- ``file_deleted``: the commit removes a file.
- ``workflow_changed``: a file under .github/workflows/ changes.
- ``infrastructure_changed``: a file under an infra/ or cdk/ directory,
  a cdk.json, or a Terraform file (.tf, .tfvars) changes.
- ``database_migration``: a file under a migrations/, migration/,
  alembic/ or migrate/ directory changes, or an added line holds a DROP
  statement (DROP TABLE, DATABASE, SCHEMA, COLUMN, INDEX or VIEW).
- ``secret_in_added_line``: an added line matches one of SECRET_PATTERNS.
- ``scan_incomplete``: the commit was not read in full, either because
  its added lines passed SCAN_COMMIT_CHARS (from the first file past the
  budget on, files are checked by path only) or because GitHub listed
  more files than the ingest read (domain.build adds that one). It flags
  the commit: what was not read is not called clean.

A renamed file is checked by its new path and by the path it had, so a
workflow moved out of .github/workflows/ (which turns it off) or a file
moved out of infra/ raises the rule of its old path, with that path.

The bounds are counted in characters, so the same commit always scans
the same way: at most SCAN_CHARS of the added lines of each file (the
rest of a longer file is not read) and at most SCAN_COMMIT_CHARS of added
lines per commit. Before the patterns run, every run of more than
RUN_CHARS word characters or hyphens is cut to its first RUN_CHARS (see
bounded()). One copied pattern backtracks quadratically over such a run,
so the cut keeps each match cheap, and because the cut stops at the
run's own end, a long quoted secret (a JWT assigned to ``token``) still
ends at its closing quote and is still found. The caller can also stop a
scan between two files (OutOfTime), for the Lambda deadline.

SECRET_PATTERNS is a copy of lachesis.policy.SECRET_PATTERNS, kept here
because the Lambda asset does not ship src/lachesis.
tests/test_activity_scan_mirror.py fails if the two drift apart.

A commit's verdict is ``flagged`` when it has any finding and ``clean``
otherwise. A file without a patch (binary, or too large for GitHub to
return one) is checked by its path only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

LABEL = "deterministic scan"
CLEAN = "clean"
FLAGGED = "flagged"
VERDICTS = (CLEAN, FLAGGED)

FILE_DELETED = "file_deleted"
WORKFLOW_CHANGED = "workflow_changed"
INFRASTRUCTURE_CHANGED = "infrastructure_changed"
DATABASE_MIGRATION = "database_migration"
SECRET_IN_ADDED_LINE = "secret_in_added_line"
SCAN_INCOMPLETE = "scan_incomplete"
RULES = (
    FILE_DELETED,
    WORKFLOW_CHANGED,
    INFRASTRUCTURE_CHANGED,
    DATABASE_MIGRATION,
    SECRET_IN_ADDED_LINE,
    SCAN_INCOMPLETE,
)

# Copied line for line from src/lachesis/policy.py (the mirror test pins it).
SECRET_PATTERNS = (
    ("an AWS access key id", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")),
    ("an sk- secret key", re.compile(r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9_\-]{20,}")),
    ("a GitHub token", re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36,}")),
    ("a Slack token", re.compile(r"(?<![A-Za-z0-9_])xox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("a Google API key", re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_\-]{35}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a long secret assigned to a secret-like name", re.compile(
        r"(?i)\b(?:password|passwd|secret|token|api[_\-]?key)\b[\"'\']?\s*[:=]\s*[\"'\'][^\"'\s]{20,}[\"'\']")),
    ("a long value assigned to a key-like name", re.compile(
        r"(?i)\b[\w\-]*(?:key|secret|token|password|passwd)[\w\-]*\b[\"'\']?\s*[:=]\s*[\"'\'][^\"'\s]{16,}[\"'\']")),
    ("a password inside a connection string", re.compile(r"://[^/\s:@]{1,64}:[^/\s@]{1,64}@")),
    ("a bearer token", re.compile(r"(?i)bearer [A-Za-z0-9_\-\.~+/]{4,}={0,2}")),
)

# Per file, per commit, and per pattern match (see the module docstring).
SCAN_CHARS = 20000
SCAN_COMMIT_CHARS = 400_000
RUN_CHARS = 256
BUDGET_REASON = (
    "the scan reached its per-commit budget here; this file and the ones after it "
    "were checked by path only"
)

_INFRA_DIRS = frozenset({"infra", "cdk"})
_INFRA_SUFFIXES = (".tf", ".tfvars")
_MIGRATION_DIRS = frozenset({"migrations", "migration", "alembic", "migrate"})
_DROP = re.compile(r"(?i)\bDROP\s+(?:TABLE|DATABASE|SCHEMA|COLUMN|INDEX|VIEW)\b")
_ORDER = {rule: index for index, rule in enumerate(RULES)}
_LONG_RUN = re.compile(r"[\w\-]{" + str(RUN_CHARS + 1) + ",}")


class OutOfTime(Exception):
    """The caller's deadline passed between two files of a commit."""


@dataclass(frozen=True)
class FileChange:
    """One changed file of a commit: its path, GitHub's status for it
    (added, modified, removed, renamed, copied, changed, unchanged), its
    unified diff when GitHub returned one, and for a rename the path it
    had. The patch is scanned and then dropped: it is never stored."""

    path: str
    status: str
    patch: str = ""
    previous_path: str = ""


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "path": self.path, "reason": self.reason}


def added_text(patch: str) -> tuple[str, bool]:
    """The added lines of a GitHub ``patch``, without their leading +,
    capped at SCAN_CHARS characters, and whether the cap cut them.

    GitHub's per-file ``patch`` holds hunks only, never the ---/+++ file
    headers, so every line that starts with + is an added line, also one
    whose own text starts with ++. Lines are split on newline only: a
    form feed, a file or group separator, NEL or a Unicode line separator
    inside an added line is part of that line, never the start of a line
    that could escape the scan."""
    kept: list[str] = []
    size = 0
    cut = False
    lines = (patch or "").split("\n")
    for index, line in enumerate(lines):
        if not line.startswith("+"):
            continue
        text = line[1:]
        kept.append(text)
        size += len(text) + 1
        if size >= SCAN_CHARS:
            cut = size - 1 > SCAN_CHARS or any(
                rest.startswith("+") for rest in lines[index + 1 :]
            )
            break
    return "\n".join(kept)[:SCAN_CHARS], cut


def added_lines(patch: str) -> str:
    """The added lines of a unified diff, without their leading +, capped
    at SCAN_CHARS characters (see added_text)."""
    return added_text(patch)[0]


def secret_hit(text: str) -> str | None:
    """The name of the first secret pattern the text matches, or None."""
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return name
    return None


def bounded(text: str) -> str:
    """The text with every run of more than RUN_CHARS word characters or
    hyphens cut to its first RUN_CHARS."""
    return _LONG_RUN.sub(lambda match: match.group(0)[:RUN_CHARS], str(text or ""))


def secret_in_text(text: str) -> str | None:
    """secret_hit on bounded(text)."""
    return secret_hit(bounded(text))


def _dirs(path: str) -> list[str]:
    return [part.lower() for part in path.split("/")[:-1]]


def _path_rules(path: str) -> list[tuple[str, str]]:
    """The path rules one path raises, as (rule, reason), in RULES order."""
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    dirs = _dirs(path)
    hits = []
    if lowered.startswith(".github/workflows/"):
        hits.append((WORKFLOW_CHANGED, "a CI workflow changed"))
    if _INFRA_DIRS.intersection(dirs) or name == "cdk.json" or name.endswith(_INFRA_SUFFIXES):
        hits.append((INFRASTRUCTURE_CHANGED, "an infrastructure file changed"))
    if _MIGRATION_DIRS.intersection(dirs):
        hits.append((DATABASE_MIGRATION, "a database migration changed"))
    return hits


def scan_file(change: FileChange, read_lines: bool = True) -> list[Finding]:
    """The findings of one file. With ``read_lines`` False only its path
    (and, for a rename, its old path) is checked."""
    path = change.path
    found: list[Finding] = []
    if change.status == "removed":
        found.append(Finding(FILE_DELETED, path, "the commit deletes this file"))
    rules = [(rule, path, reason) for rule, reason in _path_rules(path)]
    old = change.previous_path if change.status == "renamed" else ""
    if old and old != path:
        by_new = {rule for rule, _, _ in rules}
        rules += [(rule, old, reason + " (moved away by a rename)")
                  for rule, reason in _path_rules(old) if rule not in by_new]
    for rule, where, reason in sorted(rules, key=lambda item: _ORDER[item[0]]):
        found.append(Finding(rule, where, reason))
    if not read_lines:
        return found
    found.extend(text_findings(path, added_lines(change.patch), found))
    return found


def text_findings(path: str, added: str, found: Iterable[Finding] = ()) -> list[Finding]:
    """The line rules on ``added``, the added text of one file as it is
    (no diff markers): a DROP statement (unless ``found`` already holds a
    database_migration finding) and a secret pattern."""
    out: list[Finding] = []
    if all(f.rule != DATABASE_MIGRATION for f in found) and _DROP.search(added):
        out.append(Finding(DATABASE_MIGRATION, path, "an added line holds a DROP statement"))
    hit = secret_in_text(added)
    if hit is not None:
        out.append(Finding(SECRET_IN_ADDED_LINE, path, f"an added line contains {hit}"))
    return out


def scan(
    changes: Iterable[FileChange], out_of_time: Callable[[], bool] | None = None
) -> list[Finding]:
    """Every finding of a commit, file by file, in the order GitHub listed
    the files and, within a file, in RULES order. Once the added lines
    pass SCAN_COMMIT_CHARS, one scan_incomplete finding names the first
    file not read, and it and every file after it are checked by path
    only. Raises OutOfTime when ``out_of_time`` says so before a file."""
    found: list[Finding] = []
    budget, spent = SCAN_COMMIT_CHARS, False
    for change in changes:
        if out_of_time is not None and out_of_time():
            raise OutOfTime()
        size = len(added_lines(change.patch)) if change.patch else 0
        if not spent and size > budget:
            spent = True
            found.append(Finding(SCAN_INCOMPLETE, change.path, BUDGET_REASON))
        found.extend(scan_file(change, read_lines=not spent))
        if not spent:
            budget -= size
    return found


def verdict(findings: Iterable[Finding]) -> str:
    return FLAGGED if any(True for _ in findings) else CLEAN

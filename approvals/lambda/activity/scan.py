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

SECRET_PATTERNS is a copy of lachesis.policy.SECRET_PATTERNS, kept here
because the Lambda asset does not ship src/lachesis.
tests/test_activity_scan_mirror.py fails if the two drift apart.

A commit's verdict is ``flagged`` when it has any finding and ``clean``
otherwise. A file without a patch (binary, or too large for GitHub to
return one) is checked by its path only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
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
RULES = (
    FILE_DELETED,
    WORKFLOW_CHANGED,
    INFRASTRUCTURE_CHANGED,
    DATABASE_MIGRATION,
    SECRET_IN_ADDED_LINE,
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

# The scan reads at most this many characters of added lines per file.
SCAN_CHARS = 20000

_INFRA_DIRS = frozenset({"infra", "cdk"})
_INFRA_SUFFIXES = (".tf", ".tfvars")
_MIGRATION_DIRS = frozenset({"migrations", "migration", "alembic", "migrate"})
_DROP = re.compile(r"(?i)\bDROP\s+(?:TABLE|DATABASE|SCHEMA|COLUMN|INDEX|VIEW)\b")


@dataclass(frozen=True)
class FileChange:
    """One changed file of a commit: its path, GitHub's status for it
    (added, modified, removed, renamed, copied, changed, unchanged), and
    its unified diff when GitHub returned one. The patch is scanned and
    then dropped: it is never stored."""

    path: str
    status: str
    patch: str = ""


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "path": self.path, "reason": self.reason}


def added_lines(patch: str) -> str:
    """The added lines of a unified diff, without their leading +, capped
    at SCAN_CHARS characters."""
    kept: list[str] = []
    size = 0
    for line in (patch or "").splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        text = line[1:]
        kept.append(text)
        size += len(text) + 1
        if size >= SCAN_CHARS:
            break
    return "\n".join(kept)[:SCAN_CHARS]


def secret_hit(text: str) -> str | None:
    """The name of the first secret pattern the text matches, or None."""
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return name
    return None


def _dirs(path: str) -> list[str]:
    return [part.lower() for part in path.split("/")[:-1]]


def scan_file(change: FileChange) -> list[Finding]:
    path = change.path
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    dirs = _dirs(path)
    found: list[Finding] = []
    if change.status == "removed":
        found.append(Finding(FILE_DELETED, path, "the commit deletes this file"))
    if lowered.startswith(".github/workflows/"):
        found.append(Finding(WORKFLOW_CHANGED, path, "a CI workflow changed"))
    if _INFRA_DIRS.intersection(dirs) or name == "cdk.json" or name.endswith(_INFRA_SUFFIXES):
        found.append(Finding(INFRASTRUCTURE_CHANGED, path, "an infrastructure file changed"))
    added = added_lines(change.patch)
    if _MIGRATION_DIRS.intersection(dirs):
        found.append(Finding(DATABASE_MIGRATION, path, "a database migration changed"))
    elif _DROP.search(added):
        found.append(Finding(DATABASE_MIGRATION, path, "an added line holds a DROP statement"))
    hit = secret_hit(added)
    if hit is not None:
        found.append(Finding(SECRET_IN_ADDED_LINE, path, f"an added line contains {hit}"))
    return found


def scan(changes: Iterable[FileChange]) -> list[Finding]:
    """Every finding of a commit, file by file, in the order GitHub listed
    the files and, within a file, in RULES order."""
    return [finding for change in changes for finding in scan_file(change)]


def verdict(findings: Iterable[Finding]) -> str:
    return FLAGGED if any(True for _ in findings) else CLEAN

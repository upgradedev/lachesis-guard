"""The Activity value of the Activity bounded context (B7a). Pure Python.

An Activity is one commit of a registered repository: who made it, which
coding agents the commit names as co-authors, which files changed, and
what the deterministic scan (activity/scan.py) found in it.

Actor attribution, in this order:

- ``bot``: the GitHub account that authored the commit is a Bot (its
  type is Bot, or its login or git name ends in "[bot]").
- ``agent-assisted``: a Co-Authored-By trailer names a coding agent,
  mapped exactly by AGENTS below.
- ``human``: anything else.

A bot commit that also names an agent stays ``bot``; the agent is still
listed in co_author_agents.

The trailer mapping is exact on the first word of the co-author's name,
case-insensitive: Claude is Claude; Codex or OpenAI is Codex; Gemini or
Antigravity is Gemini. Nothing else counts, so "Claudette" is no agent,
and a person whose first name is Claude would be counted as the agent
(a known limit of name-based attribution). The address part of a trailer
is never kept.

Privacy: the commit author's address is never read, and the message is
cut to its subject line with anything address-shaped replaced, so no
address reaches the table, the bus or the API.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from activity import scan

HUMAN = "human"
AGENT_ASSISTED = "agent-assisted"
BOT = "bot"
ACTOR_KINDS = (HUMAN, AGENT_ASSISTED, BOT)

# First word of a Co-Authored-By name (lowercase) -> the agent it names.
AGENTS: dict[str, str] = {
    "claude": "Claude",
    "codex": "Codex",
    "openai": "Codex",
    "gemini": "Gemini",
    "antigravity": "Gemini",
}
AGENT_NAMES = ("Claude", "Codex", "Gemini")

SUBJECT_CHARS = 200
NAME_CHARS = 100
MAX_FILES = 300

_TRAILER = re.compile(r"^\s*co-authored-by\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_ADDRESS = re.compile(r"\S+@\S+")
_FIRST_WORD = re.compile(r"[A-Za-z]+")


def scrub(text: str, limit: int) -> str:
    """One line of text with anything address-shaped replaced, capped."""
    line = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    return _ADDRESS.sub("[address]", line)[:limit]


def subject_of(message: str) -> str:
    first = str(message or "").split("\n", 1)[0]
    return scrub(first, SUBJECT_CHARS)


def agent_of(trailer_value: str) -> str | None:
    """The agent a Co-Authored-By value names, or None. Only the name part
    (before any <address>) is read."""
    name = str(trailer_value or "").split("<", 1)[0].strip()
    match = _FIRST_WORD.match(name)
    return AGENTS.get(match.group(0).lower()) if match else None


def co_author_agents(message: str) -> tuple[str, ...]:
    """The agents a commit message's Co-Authored-By trailers name, in
    AGENT_NAMES order, each once."""
    found = {agent_of(value) for value in _TRAILER.findall(str(message or ""))}
    return tuple(name for name in AGENT_NAMES if name in found)


def is_bot(author: dict[str, Any] | None, git_name: str) -> bool:
    author = author if isinstance(author, dict) else {}
    login = str(author.get("login") or "")
    return (
        str(author.get("type") or "") == "Bot"
        or login.lower().endswith("[bot]")
        or str(git_name or "").lower().endswith("[bot]")
    )


def actor_kind(bot: bool, agents: Iterable[str]) -> str:
    if bot:
        return BOT
    return AGENT_ASSISTED if tuple(agents) else HUMAN


def iso_utc(value: str) -> str:
    """A GitHub timestamp as YYYY-MM-DDTHH:MM:SSZ (UTC). Raises ValueError
    on anything that is not an ISO 8601 date-time."""
    moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_of(iso: str) -> int:
    return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def iso_of(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@dataclass(frozen=True)
class Activity:
    project: str
    repo: str
    sha: str
    committed_at: str
    actor_login: str
    actor_name: str
    actor_kind: str
    co_author_agents: tuple[str, ...]
    subject: str
    files: tuple[scan.FileChange, ...]
    findings: tuple[scan.Finding, ...]
    files_changed: int = 0

    @property
    def scan_verdict(self) -> str:
        return scan.verdict(self.findings)

    @property
    def project_repo(self) -> str:
        return f"{self.project}#{self.repo}"

    @property
    def sort_key(self) -> str:
        return f"{self.committed_at}#{self.sha}"

    @property
    def files_truncated(self) -> bool:
        return self.files_changed > len(self.files)


def build(project: str, repo: str, commit: dict[str, Any], files: list[dict]) -> Activity:
    """One Activity from GitHub's commit object (GET /repos/{r}/commits/{sha})
    and the files of all its pages. The anti-corruption step: only the
    fields named here are read, and no address field is touched.

    committed_at is the committer date, the one GitHub's ``since`` filter
    and the per-repo cursor use."""
    sha = str(commit.get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("commit without a 40-hex sha")
    git = commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
    git_author = git.get("author") if isinstance(git.get("author"), dict) else {}
    git_committer = git.get("committer") if isinstance(git.get("committer"), dict) else {}
    when = git_committer.get("date") or git_author.get("date")
    if not when:
        raise ValueError("commit without a date")
    author = commit.get("author") if isinstance(commit.get("author"), dict) else None
    git_name = str(git_author.get("name") or "")
    message = str(git.get("message") or "")
    agents = co_author_agents(message)
    changes = []
    for entry in files:
        if not isinstance(entry, dict) or not entry.get("filename"):
            continue
        changes.append(
            scan.FileChange(
                path=str(entry["filename"]),
                status=str(entry.get("status") or "modified"),
                patch=str(entry.get("patch") or ""),
            )
        )
    findings = scan.scan(changes)
    kept = tuple(scan.FileChange(change.path, change.status) for change in changes[:MAX_FILES])
    return Activity(
        project=project,
        repo=repo,
        sha=sha,
        committed_at=iso_utc(when),
        actor_login=scrub((author or {}).get("login") or "", NAME_CHARS),
        actor_name=scrub(git_name, NAME_CHARS),
        actor_kind=actor_kind(is_bot(author, git_name), agents),
        co_author_agents=agents,
        subject=subject_of(message),
        files=kept,
        findings=tuple(findings),
        files_changed=len(changes),
    )

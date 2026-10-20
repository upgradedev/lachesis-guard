"""The merge gate's request contract, scan and card (B7b). Pure Python.

The request (POST /gate/pulls) is

    {"repository": "owner/name", "pr_number": 12, "head_sha": "<40 hex>",
     "files_truncated": false,
     "changes": [{"path", "status", "additions", "deletions",
                  "added_lines_excerpt", "excerpt_truncated"?, "previous_path"?}]}

and it is refused before any scan when it is larger than MAX_BODY_BYTES
(413), lists more than MAX_FILES changes (413), or fails validation (400).
An excerpt longer than MAX_EXCERPT_CHARS, or a path longer than
MAX_PATH_CHARS, is cut here and flagged, never refused, so a large pull
request always reaches a person.

Each change is checked by activity.scan, the deterministic scan the
commit ingest uses, with the excerpt as its added lines. Beside the scan
rules, the gate adds scan_incomplete whenever part of a change was not
read: the workflow cut the file list or an excerpt, this module cut an
excerpt or a path, the commit budget of the scan ran out, or GitHub returned no
diff for a file that adds lines. What was not read is never called
clean.

The gate passes only when no change has a finding; otherwise a person
decides. This is the deterministic scan's verdict, labelled as such: a
Decider reading (gate/decider.py) is advisory and is shown beside it,
never in place of it.

The card a person decides on lists the flagged changes (path, rules and
fixed reasons, never a matched line) and goes only to the approver lane.
The summary GET /gate/{id} answers with holds counts only: no path, no
reason, no excerpt.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from activity import registry, scan

LABEL = scan.LABEL
LAYER = "mergegate"
TOOL = "merge_gate"

MAX_BODY_BYTES = 512 * 1024
MAX_FILES = 300
MAX_EXCERPT_CHARS = 8000
MAX_PATH_CHARS = 400
# Flagged changes the card lists by name; the rest are counted.
CARD_CHANGES = 20
# Gate runs per pull-request head: an EXPIRED or ERROR run may be retried
# under the next round's id, at most this many times.
MAX_ROUNDS = 5

STATUSES = frozenset(
    {"added", "modified", "removed", "renamed", "copied", "changed", "unchanged"}
)
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

FILES_NOT_SENT = "(files not sent)"
REASON_LIST_CUT = "the workflow sent only part of the file list; the rest was not read"
REASON_EXCERPT_CUT = "the added lines of this file were cut before the scan; the rest was not read"
REASON_PATH_CUT = f"the path was longer than {MAX_PATH_CHARS} characters and was cut"
REASON_NO_DIFF = (
    "GitHub returned no diff for this file although it adds lines (too large); "
    "it was checked by path only"
)
DECIDER_ASLEEP = "Decider asleep: a person decides"


class Refused(Exception):
    """A request the gate refuses before any scan. The message is public."""

    def __init__(self, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class Change:
    path: str
    status: str
    additions: int
    deletions: int
    excerpt: str
    previous_path: str = ""
    excerpt_truncated: bool = False
    path_truncated: bool = False


@dataclass(frozen=True)
class PullRequest:
    repository: str
    pr_number: int
    head_sha: str
    changes: tuple[Change, ...]
    files_truncated: bool = False


@dataclass(frozen=True)
class ChangeVerdict:
    change: Change
    findings: tuple[scan.Finding, ...]

    @property
    def flagged(self) -> bool:
        return bool(self.findings)

    @property
    def rules(self) -> tuple[str, ...]:
        seen: list[str] = []
        for finding in self.findings:
            if finding.rule not in seen:
                seen.append(finding.rule)
        return tuple(seen)


@dataclass(frozen=True)
class GateScan:
    verdicts: tuple[ChangeVerdict, ...]
    extra: tuple[scan.Finding, ...] = field(default_factory=tuple)

    @property
    def flagged(self) -> tuple[ChangeVerdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.flagged)

    @property
    def needs_person(self) -> bool:
        return bool(self.flagged or self.extra)


# --- the request ----------------------------------------------------------


def _count(value: Any, name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Refused(f"{name} must be a whole number of at least 0")
    return value


def _flag(value: Any, name: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise Refused(f"{name} must be true or false")
    return value


def _path(value: Any, name: str, required: bool) -> tuple[str, bool]:
    """The path, cut to MAX_PATH_CHARS, and whether it was cut. A cut
    path is flagged scan_incomplete rather than refused, so a pull
    request with one long path still reaches a person."""
    if value is None or value == "":
        if required:
            raise Refused(f"{name} is required")
        return "", False
    if not isinstance(value, str) or "\x00" in value:
        raise Refused(f"{name} must be a path")
    if len(value) > MAX_PATH_CHARS:
        return value[:MAX_PATH_CHARS], True
    return value, False


def _change(entry: Any) -> Change:
    if not isinstance(entry, dict):
        raise Refused("each change must be a JSON object")
    status = entry.get("status")
    if status not in STATUSES:
        raise Refused("change status must be one of GitHub's file statuses")
    excerpt = entry.get("added_lines_excerpt")
    if excerpt is None:
        excerpt = ""
    if not isinstance(excerpt, str):
        raise Refused("added_lines_excerpt must be a string")
    truncated = _flag(entry.get("excerpt_truncated"), "excerpt_truncated")
    if len(excerpt) > MAX_EXCERPT_CHARS:
        excerpt, truncated = excerpt[:MAX_EXCERPT_CHARS], True
    path, path_cut = _path(entry.get("path"), "change path", True)
    previous, previous_cut = _path(entry.get("previous_path"), "previous_path", False)
    return Change(
        path=path,
        status=status,
        additions=_count(entry.get("additions"), "additions"),
        deletions=_count(entry.get("deletions"), "deletions"),
        excerpt=excerpt,
        previous_path=previous,
        excerpt_truncated=truncated,
        path_truncated=path_cut or previous_cut,
    )


def parse(body: Any) -> PullRequest:
    """The pull request a parsed body describes. Raises Refused."""
    if not isinstance(body, dict):
        raise Refused("request body must be a JSON object")
    repository = body.get("repository")
    if not isinstance(repository, str) or not registry.valid_repo(repository):
        raise Refused("repository must be owner/name")
    number = body.get("pr_number")
    if isinstance(number, bool) or not isinstance(number, int) or not 0 < number < 10**9:
        raise Refused("pr_number must be a positive whole number")
    head_sha = body.get("head_sha")
    if not isinstance(head_sha, str) or not _SHA.fullmatch(head_sha):
        raise Refused("head_sha must be a full lowercase commit sha")
    changes = body.get("changes")
    if not isinstance(changes, list):
        raise Refused("changes must be a list")
    if len(changes) > MAX_FILES:
        raise Refused(f"at most {MAX_FILES} changes per request", 413)
    return PullRequest(
        repository=repository,
        pr_number=number,
        head_sha=head_sha,
        changes=tuple(_change(entry) for entry in changes),
        files_truncated=_flag(body.get("files_truncated"), "files_truncated"),
    )


# --- the scan -------------------------------------------------------------


def _pseudo_patch(excerpt: str) -> str:
    """The excerpt as the added lines of a unified diff, for activity.scan."""
    return "\n".join("+" + line for line in excerpt.splitlines())


def scan_pull(pull: PullRequest) -> GateScan:
    """Every change through the deterministic scan, under the scan's own
    per-commit budget, plus the gate's scan_incomplete findings."""
    verdicts: list[ChangeVerdict] = []
    budget, spent = scan.SCAN_COMMIT_CHARS, False
    for change in pull.changes:
        file_change = scan.FileChange(
            path=change.path,
            status=change.status,
            patch=_pseudo_patch(change.excerpt),
            previous_path=change.previous_path,
        )
        size = len(scan.added_lines(file_change.patch)) if file_change.patch else 0
        found: list[scan.Finding] = []
        if not spent and size > budget:
            spent = True
            found.append(scan.Finding(scan.SCAN_INCOMPLETE, change.path, scan.BUDGET_REASON))
        found.extend(scan.scan_file(file_change, read_lines=not spent))
        if not spent:
            budget -= size
        if change.path_truncated:
            found.append(scan.Finding(scan.SCAN_INCOMPLETE, change.path, REASON_PATH_CUT))
        if change.excerpt_truncated:
            found.append(scan.Finding(scan.SCAN_INCOMPLETE, change.path, REASON_EXCERPT_CUT))
        elif change.additions > 0 and not change.excerpt and change.status != "removed":
            found.append(scan.Finding(scan.SCAN_INCOMPLETE, change.path, REASON_NO_DIFF))
        verdicts.append(ChangeVerdict(change, tuple(found)))
    extra = (
        (scan.Finding(scan.SCAN_INCOMPLETE, FILES_NOT_SENT, REASON_LIST_CUT),)
        if pull.files_truncated
        else ()
    )
    return GateScan(tuple(verdicts), extra)


# --- what is recorded -----------------------------------------------------


def gate_id(pull: PullRequest, round_: int) -> str:
    """The approval id of a pull-request head's gate run in ``round_``.
    Derived, so a re-run of the job finds the same run (and its decision)
    instead of opening a second card. Knowing it grants nothing: every
    route that reads or decides by id checks the lane and the caller."""
    digest = hashlib.sha256(
        f"{pull.repository}#{pull.pr_number}#{pull.head_sha}".encode()
    ).hexdigest()[:40]
    return f"appr-gate-{digest}-{int(round_)}"


def summary(result: GateScan, decider: str) -> dict[str, Any]:
    """The verdict summary GET /gate/{id} may show: counts only.
    ``decider`` is "not_needed", "asleep", "answered" or "failed"."""
    by_rule = dict.fromkeys(scan.RULES, 0)
    for verdict in result.verdicts:
        for rule in verdict.rules:
            by_rule[rule] += 1
    for finding in result.extra:
        by_rule[finding.rule] += 1
    return {
        "label": LABEL,
        "verdict": scan.FLAGGED if result.needs_person else scan.CLEAN,
        "files": len(result.verdicts),
        "flagged_files": len(result.flagged),
        "files_by_rule": {rule: count for rule, count in by_rule.items() if count},
        "decider": decider,
    }


def card(
    pull: PullRequest,
    project: str,
    result: GateScan,
    decider_line: str,
    created_at: str,
    expires_at: str,
) -> dict[str, Any]:
    """The approver card of a held gate run. ``reason`` is plain text: the
    console shows it as is. It names paths, so it is never on an
    anonymous route. ``risk`` and ``confidence`` stay empty: the pause is
    the deterministic scan's, not a Decider verdict."""
    flagged = result.flagged
    lines = [
        (
            f"Merge gate: pull request #{pull.pr_number} in {pull.repository} ({project}), "
            f"head {pull.head_sha[:12]}."
        ),
        (
            f"The {LABEL} flagged {len(flagged)} of {len(result.verdicts)} changed files; "
            "a person decides whether it may merge."
        ),
        "",
    ]
    for verdict in flagged[:CARD_CHANGES]:
        change = verdict.change
        lines.append(f"{change.path} ({change.status}, +{change.additions} -{change.deletions})")
        for finding in verdict.findings:
            where = "" if finding.path == change.path else f" [{finding.path}]"
            lines.append(f"  {finding.rule}: {finding.reason}{where}")
    if len(flagged) > CARD_CHANGES:
        lines.append(f"... and {len(flagged) - CARD_CHANGES} more flagged files.")
    for finding in result.extra:
        lines.append(f"{finding.path}: {finding.rule}: {finding.reason}")
    lines += ["", decider_line]
    return {
        "reason": "\n".join(lines),
        "tool": TOOL,
        "risk": "",
        "confidence": None,
        "latency_ms": None,
        "layer": LAYER,
        "changed_files": [verdict.change.path for verdict in flagged[:CARD_CHANGES]],
        "created_at": created_at,
        "expires_at": expires_at,
    }

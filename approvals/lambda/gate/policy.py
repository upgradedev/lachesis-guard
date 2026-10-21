"""The merge gate's request contract, scan and card (B7b). Pure Python.

The request (POST /gate/pulls) only names the pull request:

    {"repository": "owner/name", "pr_number": 12, "head_sha": "<40 hex>"}

It is refused when larger than MAX_BODY_BYTES (413) or malformed (400);
any other key, such as a list of changes, is ignored. The gate never
scans what the request says changed: gate_api.py reads the pull request
and its changed files from GitHub itself (activity/github.py, the
read-only token) and builds the PullRequest here with from_github(). So
a pull request's own copy of the workflow cannot choose, trim or empty
the list the gate scans.

Each change is checked by activity.scan, the deterministic scan the
commit ingest uses: its path rules, and its line rules on the added text
of GitHub's patch, scanned as it is (scan.text_findings), never through
a rebuilt diff. Beside the scan rules, the gate adds scan_incomplete
whenever part of a change was not read: GitHub listed more files than
the gate read (or none at all), a file's added text passed
scan.SCAN_CHARS, a path was cut or holds control characters (format
characters such as bidi overrides and zero-width marks included), the
scan's budget for the pull request ran out, or GitHub returned no diff for a
file that adds lines. A binary file (no additions, no patch) is checked
by its path only; its content is never read. Apart from binary files,
what was not read is never called clean.

The gate passes only when no change has a finding; otherwise a person
decides. This is the deterministic scan's verdict, labelled as such: a
Decider reading (gate/decider.py) is advisory and is shown beside it,
never in place of it.

The card a person decides on lists the flagged changes (path, rules and
fixed reasons, never a matched line) and goes only to the approver lane.
Paths and the base branch on the card are shown with control characters
escaped (shown()), so a file name cannot forge a line of the card. The
card stops listing changes before it passes CARD_MAX_BYTES as stored, so
a pull request of long non-ASCII paths still fits its DynamoDB row, and
the approver queue lists only its first lines (common.queue_card). The
summary GET /gate/{id} answers with holds counts only: no path, no
reason, no excerpt.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from activity import registry, scan

LABEL = scan.LABEL
LAYER = "mergegate"
TOOL = "merge_gate"

MAX_BODY_BYTES = 4096
# Files the gate reads from GitHub: FILE_PAGES pages of 100.
FILE_PAGES = 3
MAX_FILES = FILE_PAGES * 100
MAX_PATH_CHARS = 400
# The base branch name as the card and the queue show it (escaped by
# shown()); the run id (gate_id) holds the whole name.
MAX_REF_CHARS = 256
# Flagged changes the card lists by name; the rest are counted.
CARD_CHANGES = 20
# The card's reason and changed_files as stored (json.dumps with the
# default ensure_ascii, so an astral character costs 12 bytes) stop
# growing before the whole stored card passes this many bytes; a DynamoDB
# item holds at most 400 KB. CARD_OTHER_BYTES is kept for the card's
# other keys (tool, layer, the timestamps), which take about 250.
CARD_MAX_BYTES = 300_000
CARD_OTHER_BYTES = 1024
# Gate runs per pull-request head: an EXPIRED or ERROR run may be retried
# under the next round's id, at most this many times.
MAX_ROUNDS = 5

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# Cc: C0 and C1 controls. Cf: format characters, such as the bidi
# overrides and isolates (U+202A-202E, U+2066-2069), zero-width marks
# (U+200B-200F, U+FEFF), U+061C and the tag block, which can reorder a
# path on screen or make it look like another one. Zl, Zp: the line and
# paragraph separators.
_CONTROL = frozenset({"Cc", "Cf", "Zl", "Zp"})

FILES_NOT_SENT = "(files not read)"
REASON_LIST_CUT = "GitHub listed more changed files than the gate read; the rest were not read"
REASON_NO_FILES = "GitHub listed no changed files for this pull request; nothing was read"
REASON_EXCERPT_CUT = (
    f"the added lines of this file passed {scan.SCAN_CHARS} characters; the rest was not read"
)
REASON_PATH_CUT = f"the path was longer than {MAX_PATH_CHARS} characters and was cut"
REASON_PATH_CONTROL = "the path holds control characters; it is shown escaped, check it by hand"
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
class Request:
    """What the POST names; checked against the token and against GitHub."""

    repository: str
    pr_number: int
    head_sha: str


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
    has_patch: bool = True
    path_control: bool = False


@dataclass(frozen=True)
class PullRequest:
    repository: str
    pr_number: int
    head_sha: str
    changes: tuple[Change, ...]
    files_truncated: bool = False
    # GitHub's base branch name, cut to MAX_REF_CHARS: GitHub lists the
    # files against it, so two runs of one head are told apart by it.
    base_ref: str = ""


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


def parse(body: Any) -> Request:
    """The pull request a parsed body names. Raises Refused."""
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
    return Request(repository=repository, pr_number=number, head_sha=head_sha)


def _is_control(ch: str) -> bool:
    return unicodedata.category(ch) in _CONTROL


def shown(text: str) -> str:
    """A path as the card shows it: every control character (C0, C1, the
    Unicode format characters such as bidi overrides and zero-width
    marks, and the line and paragraph separators) escaped, so it stays
    on one line, shows in the order it is stored, and cannot pass for a
    line of the card or for another path."""
    return "".join(
        ch.encode("unicode_escape").decode("ascii") if _is_control(ch) else ch for ch in text
    )


def _path(value: Any) -> tuple[str, bool, bool]:
    """The path cut to MAX_PATH_CHARS, whether it was cut, and whether it
    holds control characters. Both are flagged, never refused: git
    allows any byte but NUL in a file name."""
    text = value if isinstance(value, str) else ""
    control = any(_is_control(ch) for ch in text)
    if len(text) > MAX_PATH_CHARS:
        return text[:MAX_PATH_CHARS], True, control
    return text, False, control


def _number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def change_from_github(entry: dict[str, Any]) -> Change:
    """One entry of GET /pulls/{n}/files as a Change. The added text of
    its patch (scan.added_text, capped at scan.SCAN_CHARS) is read here;
    the patch itself is not kept."""
    path, path_cut, control = _path(entry.get("filename"))
    previous, previous_cut, previous_control = _path(entry.get("previous_filename"))
    status = entry.get("status")
    patch = entry.get("patch")
    has_patch = isinstance(patch, str) and patch != ""
    added, cut = scan.added_text(patch) if has_patch else ("", False)
    return Change(
        path=path or "(no path)",
        status=status if isinstance(status, str) and status else "changed",
        additions=_number(entry.get("additions")),
        deletions=_number(entry.get("deletions")),
        excerpt=added,
        previous_path=previous,
        excerpt_truncated=cut,
        path_truncated=path_cut or previous_cut,
        has_patch=has_patch,
        path_control=control or previous_control,
    )


def from_github(
    request: Request,
    head_sha: str,
    files: list[Any],
    complete: bool,
    changed_files: Any = None,
    base_ref: Any = "",
) -> PullRequest:
    """The pull request as GitHub lists it. ``head_sha`` is GitHub's own
    head and ``base_ref`` its base branch. ``complete`` False, more than
    MAX_FILES entries, or a ``changed_files`` count above the files read
    marks the list cut."""
    entries = [entry for entry in files if isinstance(entry, dict)]
    kept = entries[:MAX_FILES]
    return PullRequest(
        repository=request.repository,
        pr_number=request.pr_number,
        head_sha=head_sha,
        changes=tuple(change_from_github(entry) for entry in kept),
        files_truncated=(
            not complete or len(entries) > MAX_FILES or _number(changed_files) > len(kept)
        ),
        base_ref=base_ref[:MAX_REF_CHARS] if isinstance(base_ref, str) else "",
    )


# --- the scan -------------------------------------------------------------


def scan_pull(pull: PullRequest) -> GateScan:
    """Every change through the deterministic scan, under the scan's own
    per-commit budget, plus the gate's scan_incomplete findings. The
    line rules run on each change's added text as it is."""
    verdicts: list[ChangeVerdict] = []
    budget, spent = scan.SCAN_COMMIT_CHARS, False
    for change in pull.changes:
        file_change = scan.FileChange(
            path=change.path, status=change.status, previous_path=change.previous_path
        )
        size = len(change.excerpt)
        found: list[scan.Finding] = []
        if not spent and size > budget:
            spent = True
            found.append(scan.Finding(scan.SCAN_INCOMPLETE, change.path, scan.BUDGET_REASON))
        found.extend(scan.scan_file(file_change, read_lines=False))
        if not spent:
            found.extend(scan.text_findings(change.path, change.excerpt, found))
            budget -= size
        if change.path_truncated:
            found.append(scan.Finding(scan.SCAN_INCOMPLETE, change.path, REASON_PATH_CUT))
        if change.path_control:
            found.append(scan.Finding(scan.SCAN_INCOMPLETE, change.path, REASON_PATH_CONTROL))
        if change.excerpt_truncated:
            found.append(scan.Finding(scan.SCAN_INCOMPLETE, change.path, REASON_EXCERPT_CUT))
        elif change.additions > 0 and not change.has_patch and change.status != "removed":
            found.append(scan.Finding(scan.SCAN_INCOMPLETE, change.path, REASON_NO_DIFF))
        verdicts.append(ChangeVerdict(change, tuple(found)))
    extra: tuple[scan.Finding, ...] = ()
    if pull.files_truncated:
        extra = (scan.Finding(scan.SCAN_INCOMPLETE, FILES_NOT_SENT, REASON_LIST_CUT),)
    elif not pull.changes:
        extra = (scan.Finding(scan.SCAN_INCOMPLETE, FILES_NOT_SENT, REASON_NO_FILES),)
    return GateScan(tuple(verdicts), extra)


# --- what is recorded -----------------------------------------------------


def gate_id(pull: PullRequest | Request, round_: int, base_ref: str) -> str:
    """The approval id of the gate run of a pull-request head against the
    base branch ``base_ref`` in ``round_``. Derived, so a re-run of the
    job finds the same run (and its decision) instead of opening a second
    card. The base branch is in it because GitHub lists a pull request's
    files against its base: after a retarget the same head has another
    file list, so it gets a new run, never the old verdict. The branch
    name, not its tip sha, so a merge to the base does not orphan an
    approval. Knowing the id grants nothing: every route that reads or
    decides by id checks the lane and the caller."""
    digest = hashlib.sha256(
        f"{pull.repository}#{pull.pr_number}#{pull.head_sha}#{base_ref}".encode()
    ).hexdigest()[:40]
    return f"appr-gate-{digest}-{int(round_)}"


def summary(result: GateScan, decider: str) -> dict[str, Any]:
    """The verdict summary GET /gate/{id} may show: counts only.
    ``decider`` is one of gate/decider.py's states: "not_needed",
    "not_asked", "asleep", "skipped", "answered" or "failed"."""
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


def _stored(text: str) -> int:
    """The bytes ``text`` takes in the stored card: json.dumps with the
    default ensure_ascii, the way domain.Run.pause() stores it."""
    return len(json.dumps(text))


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
    the deterministic scan's, not a Decider verdict. The first line names
    the head and the base branch (escaped by shown()), so two runs of one
    head against two bases are told apart. At most CARD_CHANGES flagged
    changes are listed, fewer when the next one would take the stored
    reason and changed_files past CARD_MAX_BYTES; the rest are counted."""
    flagged = result.flagged
    lines = [
        (
            f"Merge gate: pull request #{pull.pr_number} in {pull.repository} ({project}), "
            f"head {pull.head_sha[:12]}"
            + (f" into {shown(pull.base_ref)}." if pull.base_ref else ".")
        ),
        (
            f"The {LABEL} flagged {len(flagged)} of {len(result.verdicts)} changed files; "
            "a person decides whether it may merge."
        ),
        "",
    ]
    tail = [f"{finding.path}: {finding.rule}: {finding.reason}" for finding in result.extra]
    tail += ["", decider_line]
    # The header, the tail, the count line and the other keys are always
    # there; each listed change adds its lines (the quotes _stored counts
    # per line stand for the "\n" that joins them) and its changed_files
    # entry with its ", " separator.
    total = CARD_OTHER_BYTES + sum(_stored(line) for line in [*lines, *tail])
    total += _stored(f"... and {len(flagged)} more flagged files.")
    listed: list[str] = []
    for verdict in flagged[:CARD_CHANGES]:
        change = verdict.change
        block = [
            (
                f"{shown(change.path)} ({shown(change.status)}, "
                f"+{change.additions} -{change.deletions})"
            )
        ]
        for finding in verdict.findings:
            where = "" if finding.path == change.path else f" [{shown(finding.path)}]"
            block.append(f"  {finding.rule}: {finding.reason}{where}")
        cost = sum(_stored(line) for line in block) + _stored(shown(change.path)) + 2
        if total + cost > CARD_MAX_BYTES:
            break
        total += cost
        lines += block
        listed.append(shown(change.path))
    if len(flagged) > len(listed):
        lines.append(f"... and {len(flagged) - len(listed)} more flagged files.")
    lines += tail
    return {
        "reason": "\n".join(lines),
        "tool": TOOL,
        "risk": "",
        "confidence": None,
        "latency_ms": None,
        "layer": LAYER,
        "changed_files": listed,
        "created_at": created_at,
        "expires_at": expires_at,
    }

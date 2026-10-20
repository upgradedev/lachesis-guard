"""The read model of the Activity context: what the project routes answer.

Two lanes, decided per repository at read time from the ``private``
flag the ingest last stored for it (a repository that becomes public
shows its details from the next ingest on, one that becomes private
hides them). A repository with no stored flag yet counts as private.

- The anonymous lane (GET /projects, GET /projects/{key}/activity): a
  public repository shows its name, last activity time, counts and
  commits in full. A private repository shows only that it exists and
  is private, and adds its counts and verdict mix to its project's: no
  repository name, no last activity time, no commit, sha, path, login,
  git name, subject line or finding reason.
- The approver lane (GET /approvals/projects, GET
  /approvals/projects/{key}/activity, JWT): every repository in full.

No lane ever shows an address: none is stored. Scan findings are the
deterministic scan's (scan_label says so); nothing here is a Decider
score. Every count is read from the repository row, where the ingest
stored it at ``counted_at``; this module adds counts up and never
invents one.
"""

from __future__ import annotations

from typing import Any

from activity import domain, registry, scan
from activity import store as activity_store

RECENT_PER_REPO = 50
RECENT_TOTAL = 50
WINDOW_DAYS = activity_store.TTL_DAYS

COMMIT_FIELDS = (
    "repo", "sha", "committed_at", "actor_login", "actor_name", "actor_kind",
    "co_author_agents", "subject", "files", "files_changed", "files_truncated",
    "findings", "scan_verdict",
)
FILE_FIELDS = ("path", "status")
FINDING_FIELDS = ("rule", "path", "reason")


def visibility_of(meta: dict[str, Any] | None) -> str:
    flag = (meta or {}).get("private")
    if flag is False:
        return "public"
    return "private" if flag is True else "unknown"


def _shown(meta: dict[str, Any] | None, approver: bool) -> bool:
    return approver or visibility_of(meta) == "public"


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _counts(metas: list[dict[str, Any] | None]) -> dict[str, Any]:
    by_kind = dict.fromkeys(domain.ACTOR_KINDS, 0)
    by_verdict = dict.fromkeys(scan.VERDICTS, 0)
    commits = flagged = 0
    truncated = False
    counted = []
    for meta in metas:
        meta = meta or {}
        if not meta.get("counted_at"):
            continue
        counted.append(str(meta["counted_at"]))
        commits += _int(meta.get("commits"))
        flagged += _int(meta.get("flagged_changes"))
        truncated = truncated or meta.get("counts_truncated") is True
        for source, target in ((meta.get("by_actor_kind"), by_kind),
                               (meta.get("by_scan_verdict"), by_verdict)):
            if isinstance(source, dict):
                for key in target:
                    target[key] += _int(source.get(key))
    return {
        "commits": commits,
        "by_actor_kind": by_kind,
        "by_scan_verdict": by_verdict,
        "flagged_changes": flagged,
        "counts_truncated": truncated,
        "counted_at": min(counted) if counted else "",
    }


def _project_visibility(visibilities: list[str]) -> str:
    """public, private, mixed, or unknown when no repository was read yet."""
    if all(v == "public" for v in visibilities):
        return "public"
    if all(v == "unknown" for v in visibilities):
        return "unknown"
    if all(v != "public" for v in visibilities):
        return "private"
    return "mixed"


def project_summary(
    project: registry.Project, store: activity_store.ActivityStore, approver: bool
) -> dict[str, Any]:
    metas = [store.get_meta(project.project_repo(repo)) for repo in project.repos]
    repos, last = [], ""
    for repo, meta in zip(project.repos, metas, strict=True):
        shown = _shown(meta, approver)
        entry = {"repo": repo if shown else "", "visibility": visibility_of(meta)}
        if shown:
            entry["last_activity_at"] = str((meta or {}).get("last_activity_at") or "")
            entry["ingest_status"] = str((meta or {}).get("status") or "")
            last = max(last, entry["last_activity_at"])
        repos.append(entry)
    return {
        "key": project.key,
        "name": project.name,
        "repos": repos,
        "visibility": _project_visibility([entry["visibility"] for entry in repos]),
        "last_activity_at": last,
        **_counts(metas),
    }


def projects_view(
    projects: tuple[registry.Project, ...],
    store: activity_store.ActivityStore,
    approver: bool,
) -> dict[str, Any]:
    return {
        "lane": "approver" if approver else "public",
        "window_days": WINDOW_DAYS,
        "scan_label": scan.LABEL,
        "projects": [project_summary(project, store, approver) for project in projects],
    }


def _commit(row: dict[str, Any]) -> dict[str, Any]:
    view = {key: row[key] for key in COMMIT_FIELDS if key in row}
    view["files"] = [
        {key: str(entry.get(key, "")) for key in FILE_FIELDS}
        for entry in row.get("files") or []
        if isinstance(entry, dict)
    ]
    view["findings"] = [
        {key: str(entry.get(key, "")) for key in FINDING_FIELDS}
        for entry in row.get("findings") or []
        if isinstance(entry, dict)
    ]
    view["co_author_agents"] = [
        str(name) for name in row.get("co_author_agents") or [] if name in domain.AGENT_NAMES
    ]
    return view


def activity_view(
    project: registry.Project,
    store: activity_store.ActivityStore,
    approver: bool,
    now: float,
) -> dict[str, Any]:
    summary = project_summary(project, store, approver)
    commits: list[dict[str, Any]] = []
    hidden = 0
    for repo in project.repos:
        project_repo = project.project_repo(repo)
        if not _shown(store.get_meta(project_repo), approver):
            hidden += 1
            continue
        commits.extend(_commit(row) for row in store.recent(project_repo, now, RECENT_PER_REPO))
    commits.sort(key=lambda c: (str(c.get("committed_at", "")), str(c.get("sha", ""))),
                 reverse=True)
    return {
        **summary,
        "lane": "approver" if approver else "public",
        "window_days": WINDOW_DAYS,
        "scan_label": scan.LABEL,
        "hidden_repos": hidden,
        "recent": commits[:RECENT_TOTAL],
    }

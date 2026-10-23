"""The ingest application service of the Activity context (B7a).

``ingest_repo`` brings one registered repository up to date:

1. GET the repository for its ``private`` flag and store the flag on the
   repository row at once, before any commit row is written: a
   repository that just became private never shows a new row on the
   anonymous lane, even when the run fails half way. It fails closed: a
   404 (GitHub's answer for a private repository the token cannot read)
   stores private true and stops there, and any other refusal (4xx)
   stores no known flag, which the read model hides too. A rate limit or
   a transport failure keeps the flag of the last read.
2. List the commits since the repository's cursor (on the first run,
   WINDOW_S back, the table's TTL window). ``since`` is inclusive, so the
   commits at the cursor time come back each run; the ones already seen
   there (``seen_at_cursor``) are skipped without a GitHub call, and so
   are the future-dated ones already handled (``future_dated``).
3. Oldest first, at most ``max_commits`` per run: GET the commit and its
   files, build the Activity, scan it, PutItem it conditioned on
   attribute_not_exists. A new row, or an existing row whose event was
   never accepted, gets one ActivityObserved on the bus and is then
   marked published. A row already published is left alone, so a
   commit's event is sent until it is accepted and not after. A store
   failure stops the run (``store_error``); a row DynamoDB refuses as too
   large is skipped and named in ``errors``, so one commit can never
   freeze its repository.
4. Move the cursor. When every listed commit was handled, to the newest
   commit time seen; when the run stopped early (the per-run cap, the
   Lambda deadline, a rate limit, a GitHub error, an event the bus did
   not accept, a store failure), to the oldest commit time not yet
   handled, which is never past a commit that was not written and
   published. A commit dated more than store.SKEW_S past the ingest's
   clock never moves the cursor: a committer date is whatever the
   committer set, and a cursor in the future would hide every later
   commit. Re-listing is harmless: the writes are idempotent. The
   repository row remembers at most MAX_FUTURE future-dated commits; the
   rest are fetched again every run, and every run logs how many
   (``future_over_cap``). The row stores that count, and a run reports
   the repository failed only when its count is above the stored one, so
   one over-cap condition fails one run, not every run after it. A run
   that stopped early saw only part of the listing, so it never lowers
   the stored count.
5. Recount the window (actor kinds, scan verdicts, findings) from the
   table and store the counts on the repository row, so GET /projects
   reads one row per repository.

A stalled repository fails the run. A run that ends github_error or
publish_failed with no commit handled and the cursor where it was adds
one to the row's ``stalled_runs``; a run that ends ok or more, or that
moved the cursor or handled a commit, sets it back to 0. From
STALL_RUNS stalled runs on (about an hour at the 15-minute schedule),
every stalled run is reported failed, so a commit GitHub never serves,
or an event the bus always refuses, raises IngestErrorsAlarm instead of
freezing the repository in silence.

The ``private`` flag is written once per run, in step 1, and never
again: the last write of a run never stores a flag read before an
overlapping run read a newer one.

Known limits: GitHub's ``since`` filters on the committer date, so a
commit whose committer date is older than the cursor when it reaches the
default branch is not listed; the merge commit itself is. That covers a
merge of a long-lived branch, which keeps its commits' dates, and also a
deliberately back-dated committer date, which anyone who can push can
set: it is an evasion path this date-based cursor does not close (a
sha-based walk of the branch would). A listing longer than
github.MAX_PAGES pages is cut to its newest commits.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from activity import bus as activity_bus
from activity import domain, github, registry, scan
from activity import store as activity_store

MAX_COMMITS_PER_RUN = 60
WINDOW_S = activity_store.WINDOW_S
# The most future-dated commits one repository row remembers. More are
# fetched again each run (idempotent) and counted in the run log line,
# which names the repository; a run whose count grew is reported failed,
# so IngestErrorsAlarm sees it. The alarm names no repository: IngestFailed
# carries only a count, and the alarm is the Lambda Errors metric.
MAX_FUTURE = 200
# Stalled runs in a row (github_error or publish_failed, nothing handled,
# the cursor held) from which the run reports the repository failed.
STALL_RUNS = 4

OK = "ok"
MORE = "more"
UNAVAILABLE = "unavailable"
RATE_LIMITED = "rate_limited"
GITHUB_ERROR = "github_error"
PUBLISH_FAILED = "publish_failed"
DEADLINE = "deadline"
STORE_ERROR = "store_error"
ERROR = "error"
AUTH_FAILED = "auth_failed"  # GitHub refused the token (401): revoked or expired
# A run with one of these, with a commit not stored, with STALL_RUNS
# stalled runs or with a future_over_cap count above its row's stored one
# is reported failed by the ingest Lambda, so IngestErrorsAlarm fires
# (the alarm names no repository; the run log line does).
FAILED = frozenset({STORE_ERROR, ERROR, AUTH_FAILED})
# The statuses that count as a stalled run (see the module docstring).
STALLS = frozenset({GITHUB_ERROR, PUBLISH_FAILED})


@dataclass
class RepoResult:
    project_repo: str
    status: str = OK
    listed: int = 0
    written: int = 0
    published: int = 0
    skipped: int = 0
    private: bool | None = None
    cursor: str = ""
    errors: list[str] = field(default_factory=list)
    stalled_runs: int = 0
    future_over_cap: int = 0  # future-dated commits the row cannot remember
    future_over_cap_before: int = 0  # the count the repository row stored

    @property
    def failed(self) -> bool:
        return (
            self.status in FAILED
            or bool(self.errors)
            or self.stalled_runs >= STALL_RUNS
            or self.future_over_cap > self.future_over_cap_before
        )

    def as_log(self) -> dict[str, Any]:
        """The run log line: counts and status only."""
        return {
            "project_repo": self.project_repo,
            "status": self.status,
            "listed": self.listed,
            "written": self.written,
            "published": self.published,
            "skipped": self.skipped,
            "commits_not_stored": len(self.errors),
            "stalled_runs": self.stalled_runs,
            "future_dated_over_cap": self.future_over_cap,
        }


def _stored_count(meta: dict[str, Any], name: str) -> int:
    count = meta.get(name)
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def _stalled_before(meta: dict[str, Any]) -> int:
    return _stored_count(meta, "stalled_runs")


def _listed_at(summary: dict[str, Any]) -> str:
    git = summary.get("commit") if isinstance(summary.get("commit"), dict) else {}
    committer = git.get("committer") if isinstance(git.get("committer"), dict) else {}
    author = git.get("author") if isinstance(git.get("author"), dict) else {}
    return domain.iso_utc(committer.get("date") or author.get("date"))


def window_counts(store: activity_store.ActivityStore, project_repo: str, now: float) -> dict:
    """The counts of one repository's commits in the window, from its rows."""
    rows, truncated = store.window_rows(
        project_repo, now, ("actor_kind", "scan_verdict", "finding_count")
    )
    by_kind = dict.fromkeys(domain.ACTOR_KINDS, 0)
    by_verdict = dict.fromkeys(scan.VERDICTS, 0)
    flagged_changes = 0
    for row in rows:
        kind = row.get("actor_kind")
        if kind in by_kind:
            by_kind[kind] += 1
        verdict = row.get("scan_verdict")
        if verdict in by_verdict:
            by_verdict[verdict] += 1
        count = row.get("finding_count")
        flagged_changes += count if isinstance(count, int) and count > 0 else 0
    return {
        "commits": len(rows),
        "by_actor_kind": by_kind,
        "by_scan_verdict": by_verdict,
        "flagged_changes": flagged_changes,
        "counts_truncated": truncated,
        "counted_at": domain.iso_of(now),
    }


def _store_failed(step: str, exc: Exception) -> str:
    code = ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code")
    print(json.dumps({"msg": "activity store failed", "step": step,
                      "error": type(exc).__name__, "code": str(code or "")}))
    return STORE_ERROR


def _handle(
    project: registry.Project,
    repo: str,
    sha: str,
    private: bool,
    gh: github.GitHub,
    store: activity_store.ActivityStore,
    bus: activity_bus.Bus,
    now: float,
    result: RepoResult,
    out_of_time: Callable[[], bool],
) -> str:
    """One listed commit: fetch, build, scan, write, publish. Returns OK
    when the commit is handled (or skipped for good and named in
    result.errors), or the status that stops the run."""
    try:
        commit, files, complete = gh.commit(repo, sha)
    except github.RateLimited:
        return RATE_LIMITED
    except github.GitHubError:
        return GITHUB_ERROR
    try:
        activity = domain.build(
            project.key, repo, commit, files, files_complete=complete, out_of_time=out_of_time
        )
    except scan.OutOfTime:
        return DEADLINE
    except (TypeError, ValueError):
        result.errors.append(sha)  # a malformed commit is skipped, not retried
        return OK
    try:
        outcome = store.put_activity(activity, now)
    except Exception as exc:
        if activity_store.item_too_large(exc):
            result.errors.append(sha)  # skipped for good, and the run reports it
            return OK
        return _store_failed("put", exc)
    if outcome == activity_store.NEW:
        result.written += 1
    if outcome == activity_store.PUBLISHED:
        return OK
    if not bus.publish(activity_bus.detail_of(activity, private, now)):
        return PUBLISH_FAILED
    try:
        store.mark_published(activity)
    except Exception as exc:
        return _store_failed("mark_published", exc)  # the next run sends it again
    result.published += 1
    return OK


def ingest_repo(
    project: registry.Project,
    repo: str,
    *,
    gh: github.GitHub,
    store: activity_store.ActivityStore,
    bus: activity_bus.Bus,
    now: float,
    max_commits: int = MAX_COMMITS_PER_RUN,
    out_of_time: Callable[[], bool] = lambda: False,
) -> RepoResult:
    project_repo = project.project_repo(repo)
    result = RepoResult(project_repo)
    meta = store.get_meta(project_repo) or {}
    base = {"project": project.key, "repo": repo, "checked_at": domain.iso_of(now)}
    try:
        info = gh.repo(repo)
    except github.NotAccessible:
        # GitHub answers 404 for a private repository the token cannot
        # read: fail closed, the anonymous lane then shows counts only.
        result.status = UNAVAILABLE
        store.save_meta(project_repo, {**base, "status": UNAVAILABLE, "private": True})
        return result
    except github.RateLimited:
        result.status = RATE_LIMITED
        store.save_meta(project_repo, {**base, "status": RATE_LIMITED})
        return result
    except github.GitHubError as exc:
        result.status = AUTH_FAILED if exc.status == 401 else GITHUB_ERROR
        fields = {**base, "status": result.status}
        if 400 <= exc.status < 500:
            fields["private"] = None  # refused: visibility unknown, so hidden
        if result.status in STALLS:  # nothing handled, the cursor held
            result.stalled_runs = _stalled_before(meta) + 1
            fields["stalled_runs"] = result.stalled_runs
        store.save_meta(project_repo, fields)
        return result
    # Only an explicit false is public: a missing flag is treated as private.
    private = info.get("private") is not False
    result.private = private
    # Stored before any commit row is written (see step 1).
    store.save_meta(project_repo, {**base, "private": private})

    since = str(meta.get("cursor") or "")
    if not since or activity_store.is_future(since, now):
        since = domain.iso_of(now - WINDOW_S)
    seen = set(meta.get("seen_at_cursor") or [])
    future = {str(entry) for entry in meta.get("future_dated") or []}  # "<when>#<sha>"
    future_shas = {entry.rsplit("#", 1)[-1] for entry in future}
    now_iso = domain.iso_of(now)
    last_activity = str(meta.get("last_activity_at") or "")
    handled: list[tuple[str, str]] = []  # (committed_at, sha), oldest first
    remaining: list[str] = []  # listed commit times not handled
    try:
        listed = gh.commits_since(repo, since)
    except github.RateLimited:
        listed, result.status = [], RATE_LIMITED
    except github.GitHubError:
        listed, result.status = [], GITHUB_ERROR
    result.listed = len(listed)

    queue: list[tuple[str, str]] = []
    for summary in reversed(listed):  # GitHub lists newest first
        sha = str(summary.get("sha") or "")
        try:
            when = _listed_at(summary)
        except (TypeError, ValueError):
            continue
        if (when == since and sha in seen) or sha in future_shas:
            result.skipped += 1
            continue
        queue.append((when, sha))
    queue.sort()

    for index, (when, sha) in enumerate(queue):
        if result.status == OK and index >= max_commits:
            result.status = MORE
        if result.status == OK and out_of_time():
            result.status = DEADLINE
        if result.status == OK:
            result.status = _handle(
                project, repo, sha, private, gh, store, bus, now, result, out_of_time
            )
        if result.status != OK:
            remaining = [item[0] for item in queue[index:]]
            break
        handled.append((when, sha))

    def dated_now(when: str) -> bool:
        return not activity_store.is_future(when, now)

    # Never past a listed commit that was not handled; otherwise the newest.
    # A future-dated commit never moves it (see step 4).
    waiting = [when for when in remaining if dated_now(when)]
    real = [when for when, _ in handled if dated_now(when)]
    cursor = min(waiting) if waiting else max([since, *real])
    for when, _ in handled:
        last_activity = max(last_activity, min(when, now_iso))
    new_seen = {sha for when, sha in handled if when == cursor}
    if cursor == since:
        new_seen |= seen
    # A future-dated commit stays remembered while a listing can return it.
    future = {entry for entry in future if entry.rsplit("#", 1)[0] >= cursor}
    future |= {f"{when}#{sha}" for when, sha in handled if not dated_now(when)}
    result.future_over_cap = max(0, len(future) - MAX_FUTURE)
    result.future_over_cap_before = _stored_count(meta, "future_over_cap")
    # Only a run that handled its whole listing saw every such commit; one
    # that stopped early keeps the higher count, so the next full run does
    # not read the same condition as new (see step 4).
    over_cap_stored = (
        result.future_over_cap
        if result.status == OK
        else max(result.future_over_cap, result.future_over_cap_before)
    )
    result.cursor = cursor
    progressed = bool(handled) or cursor != str(meta.get("cursor") or "")
    if result.status in STALLS and not progressed:
        result.stalled_runs = _stalled_before(meta) + 1
    elif result.status in (OK, MORE) or progressed:
        result.stalled_runs = 0
    else:  # a deadline, a rate limit or a store failure: neither way
        result.stalled_runs = _stalled_before(meta)
    # No "private" here: step 1 stored it, and writing the flag read at
    # the start again could undo a newer flag an overlapping run stored.
    store.save_meta(
        project_repo,
        {
            **base,
            "status": result.status,
            "cursor": cursor,
            "seen_at_cursor": sorted(new_seen),
            "future_dated": sorted(future)[:MAX_FUTURE],
            "future_over_cap": over_cap_stored,
            "last_activity_at": last_activity,
            "stalled_runs": result.stalled_runs,
            **window_counts(store, project_repo, now),
        },
    )
    return result


def ingest_all(
    projects: tuple[registry.Project, ...],
    *,
    gh: github.GitHub,
    store: activity_store.ActivityStore,
    bus: activity_bus.Bus,
    now: float,
    out_of_time: Callable[[], bool] = lambda: False,
) -> list[RepoResult]:
    """Every registered repository, in registry order. A repository whose
    ingest raised is logged by type, gets status ``error`` on its row and
    in the results (so the run is reported failed), and does not stop the
    others; a rate limit stops the run (the budget is shared)."""
    results = []
    for project in projects:
        for repo in project.repos:
            if out_of_time():
                return results
            try:
                result = ingest_repo(
                    project, repo, gh=gh, store=store, bus=bus, now=now, out_of_time=out_of_time
                )
            except Exception as exc:
                print(json.dumps({"msg": "ingest repo failed", "project": project.key,
                                  "error": type(exc).__name__}))
                result = RepoResult(project.project_repo(repo), status=ERROR)
                try:
                    store.save_meta(result.project_repo, {
                        "project": project.key, "repo": repo,
                        "checked_at": domain.iso_of(now), "status": ERROR,
                    })
                except Exception as save_exc:
                    print(json.dumps({"msg": "ingest repo status not saved",
                                      "project": project.key,
                                      "error": type(save_exc).__name__}))
            print(json.dumps({"msg": "ingest repo", **result.as_log()}, sort_keys=True))
            results.append(result)
            if result.status == RATE_LIMITED:
                return results
    return results

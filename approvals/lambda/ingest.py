"""The activity ingest Lambda (B7a): read-only GitHub to lachesis-activity.

An EventBridge schedule runs it every 15 minutes. One run:

1. reads the GitHub token from the Secrets Manager secret
   lachesis/github-readonly (GetSecretValue on that secret only). The
   secret string is the token itself, or a JSON object with a ``token``
   key. The token lives in memory for the run; it is never logged,
   stored or returned;
2. brings every repository of the committed registry (projects.json) up
   to date through activity/service.py: commits since the per-repository
   cursor, their files, the deterministic scan, an idempotent PutItem
   per commit and one ActivityObserved per new commit on lachesis-events;
3. logs one line per repository with counts and a status, and returns
   the same counts. When a repository's ingest failed (a store failure,
   an unexpected error, or a commit that could not be stored), it raises
   IngestFailed after the log line instead, so the Lambda Errors metric
   and IngestErrorsAlarm see the failed run. The function has no async
   retry (retry_attempts 0 in the stack): the next scheduled run is the
   retry.

It stops early, leaving each cursor where the work stopped, when the
Lambda has less than STOP_BEFORE_END_MS left, when GitHub's rate limit
is at the client's floor, or when GitHub refuses a request; the next
scheduled run continues. The GitHub client sends only GET requests.

Not yet deployed or run on AWS: the secret, the schedule and the first
real ingest are unverified until the first deploy.
"""

from __future__ import annotations

import json
import time
from typing import Any

from activity import bus as activity_bus
from activity import github, registry, service
from activity import store as activity_store

SECRET_ID = github.SECRET_ID
REGION = github.REGION
STOP_BEFORE_END_MS = 90_000

# The token helpers live in activity/github.py, shared with the gate.
NoToken = github.NoToken
token_from = github.token_from


class IngestFailed(RuntimeError):
    """At least one repository's ingest failed; the message is counts only."""


def read_token(secrets_client: Any = None) -> str:
    return github.read_token(secrets_client)


def handler(event: dict | None = None, context: Any = None, *, deps: dict | None = None) -> dict:
    """``deps`` is the test seam: github, store, bus, now, projects."""
    deps = deps or {}

    def out_of_time() -> bool:
        remaining = getattr(context, "get_remaining_time_in_millis", None)
        return bool(remaining) and remaining() < STOP_BEFORE_END_MS

    gh = deps.get("github")
    if gh is None:
        gh = github.GitHub(read_token())
    results = service.ingest_all(
        deps.get("projects") or registry.load(),
        gh=gh,
        store=deps.get("store") or activity_store.ActivityStore(),
        bus=deps.get("bus") or activity_bus.Bus(),
        now=deps.get("now") or time.time(),
        out_of_time=out_of_time,
    )
    summary = {
        "repos": len(results),
        "written": sum(result.written for result in results),
        "published": sum(result.published for result in results),
        "statuses": sorted({result.status for result in results}),
        "failed_repos": sum(1 for result in results if result.failed),
        "github_requests": gh.requests,
        "rate_remaining": gh.remaining,
    }
    print(json.dumps({"msg": "ingest run", **summary}, sort_keys=True))
    if summary["failed_repos"]:
        raise IngestFailed(f"{summary['failed_repos']} repositories failed their ingest")
    return summary

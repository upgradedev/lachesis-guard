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
   an unexpected error, a refused token, a commit that could not be
   stored, service.STALL_RUNS stalled runs in a row, or a count of
   future-dated commits its row cannot remember that grew since the
   row's last count), it raises IngestFailed after the log line
   instead, so the Lambda Errors metric and IngestErrorsAlarm see the
   failed run. IngestFailed carries only the number of failed
   repositories and the alarm only the metric, so the repository is
   named in its log line alone. The function has no async retry
   (retry_attempts 0 in the stack): the next scheduled run is the
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
import os
import time
from typing import Any

from activity import bus as activity_bus
from activity import github, registry, service
from activity import store as activity_store

SECRET_ID = os.environ.get("GITHUB_SECRET_ID", "lachesis/github-readonly")
REGION = os.environ.get("AWS_REGION", "eu-west-1")
STOP_BEFORE_END_MS = 90_000


class NoToken(RuntimeError):
    """The secret holds no usable token. The message never quotes it."""


class IngestFailed(RuntimeError):
    """At least one repository's ingest failed; the message is counts only."""


def token_from(secret_string: Any) -> str:
    """The token in a secret string: the raw token, or {"token": "..."}."""
    text = str(secret_string or "").strip()
    if text.startswith("{"):
        try:
            value = json.loads(text).get("token")
        except (ValueError, AttributeError):
            raise NoToken("the secret is not valid JSON") from None
        text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        raise NoToken("the secret holds no token")
    return text


def read_token(secrets_client: Any = None) -> str:
    if secrets_client is None:
        import boto3

        secrets_client = boto3.client("secretsmanager", region_name=REGION)
    response = secrets_client.get_secret_value(SecretId=SECRET_ID)
    return token_from(response.get("SecretString"))


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

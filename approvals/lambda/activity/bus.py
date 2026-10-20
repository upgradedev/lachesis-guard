"""ActivityObserved on the bus lachesis-events (B7a).

One event per new commit row, source ``lachesis.activity``, detail-type
``ActivityObserved``, with a whitelisted detail:

- always: event_id ("<project_repo>#<sha>", stable, so a re-sent event
  dedupes in the audit lake), type, at (when the ingest saw it), project,
  repo, visibility, sha, committed_at, actor_kind, co_author_agents,
  files_changed, scan_verdict, scan_label ("deterministic scan"),
  finding_count and finding_rules (every finding of the commit, also
  the ones past the row's cap; the rule names, no paths);
- only for a public repository: actor_login.

Never the message text (for any repository), a file path, a git name, or
an address. The audit rule sends the detail to the lake as one line.

The ingest writes the commit row first (published false), then puts the
event, then marks the row published. A row whose event was not accepted
is re-sent by the next run (activity/service.py), so delivery is at
least once and consumers dedupe on event_id.
"""

from __future__ import annotations

import json
import os
from typing import Any

from activity import domain, scan

SOURCE = "lachesis.activity"
DETAIL_TYPE = "ActivityObserved"
BUS_NAME = os.environ.get("EVENT_BUS_NAME", "lachesis-events")
REGION = os.environ.get("AWS_REGION", "eu-west-1")
DETAIL_FIELDS = (
    "event_id", "type", "at", "project", "repo", "visibility", "sha", "committed_at",
    "actor_kind", "co_author_agents", "files_changed", "scan_verdict", "scan_label",
    "finding_count", "finding_rules", "actor_login",
)
PUBLIC_ONLY_FIELDS = frozenset({"actor_login"})


def detail_of(activity: domain.Activity, private: bool, now: float) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "event_id": f"{activity.project_repo}#{activity.sha}",
        "type": DETAIL_TYPE,
        "at": domain.iso_of(now),
        "project": activity.project,
        "repo": activity.repo,
        "visibility": "private" if private else "public",
        "sha": activity.sha,
        "committed_at": activity.committed_at,
        "actor_kind": activity.actor_kind,
        "co_author_agents": list(activity.co_author_agents),
        "files_changed": activity.files_changed,
        "scan_verdict": activity.scan_verdict,
        "scan_label": scan.LABEL,
        "finding_count": activity.finding_count,
        "finding_rules": list(activity.finding_rules),
    }
    if not private:
        detail["actor_login"] = activity.actor_login
    return detail


def entry_of(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "Source": SOURCE,
        "DetailType": DETAIL_TYPE,
        "Detail": json.dumps(detail, sort_keys=True, separators=(",", ":"), allow_nan=False),
        "EventBusName": BUS_NAME,
    }


class Bus:
    def __init__(self, events_client: Any = None) -> None:
        self._client = events_client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "events",
                region_name=REGION,
                config=Config(
                    connect_timeout=5,
                    read_timeout=10,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        return self._client

    def publish(self, detail: dict[str, Any]) -> bool:
        """True only when PutEvents accepted the entry (it has an EventId)."""
        try:
            response = self.client.put_events(Entries=[entry_of(detail)])
        except Exception as exc:
            print(json.dumps({"msg": "activity put_events raised", "error": type(exc).__name__}))
            return False
        results = response.get("Entries") or [{}]
        accepted = bool(results[0].get("EventId")) and not results[0].get("ErrorCode")
        if not accepted:
            print(json.dumps({"msg": "activity entry failed", "event_id": detail["event_id"],
                              "error_code": str(results[0].get("ErrorCode") or "missing")}))
        return accepted

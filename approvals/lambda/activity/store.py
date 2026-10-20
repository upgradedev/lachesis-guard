"""The DynamoDB adapter of the Activity context: the table lachesis-activity.

Keys: pk ``project_repo`` ("<project key>#<owner>/<name>"), sk ``sk``.

- A commit row has sk "<committed_at>#<sha>" (committed_at as
  YYYY-MM-DDTHH:MM:SSZ), so a repository's rows sort by commit time. It
  is written once, with a PutItem conditioned on attribute_not_exists,
  so a re-listed commit is never written twice. Its ``ttl`` is the
  commit time plus TTL_DAYS, or the ingest time plus TTL_DAYS for a
  commit dated after the ingest (``future_dated``: a committer date
  anyone can set), so such a row never outlives the window.
- The repository row has sk "#meta": the GitHub ``private`` flag, the
  ingest cursor, the last activity time and the window counts. "#" sorts
  before every digit, so every commit-row read is a Query with
  sk > the window floor, which never returns it.

Stored per commit: the project, repository, sha, commit time, actor login
and git name, actor kind, co-author agents, subject line, the changed
files (path and status, at most domain.MAX_FILES), the scan findings
(at most domain.MAX_FINDINGS, with finding_count, finding_rules and
findings_truncated covering all of them), and ``published`` (whether its
ActivityObserved event was accepted). No patch, no message body, no
address. domain.build keeps a row far below DynamoDB's 400 KB limit.

The client is the DynamoDB resource's client, which serializes plain
Python values (no floats are written here). It is created on first use.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from activity import domain

TABLE = os.environ.get("ACTIVITY_TABLE", "lachesis-activity")
REGION = os.environ.get("AWS_REGION", "eu-west-1")
META_SK = "#meta"
TTL_DAYS = 30
WINDOW_S = TTL_DAYS * 86400
# The most rows one window read returns per repository.
MAX_WINDOW_ROWS = 5000
# A commit dated more than this past the ingest's clock is future_dated.
SKEW_S = 300

NEW = "new"
UNPUBLISHED = "unpublished"
PUBLISHED = "published"

_client: Any = None


def client() -> Any:
    global _client
    if _client is None:
        import boto3

        _client = boto3.resource("dynamodb", region_name=REGION).meta.client
    return _client


def plain(value: Any) -> Any:
    """DynamoDB's Decimal back to int (every number stored here is whole)."""
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def window_floor(now: float) -> str:
    """The lowest sk inside the window: commits older than this are out."""
    return domain.iso_of(now - WINDOW_S)


def item_of(activity: domain.Activity, now: float) -> dict[str, Any]:
    return {
        "project_repo": activity.project_repo,
        "sk": activity.sort_key,
        "project": activity.project,
        "repo": activity.repo,
        "sha": activity.sha,
        "committed_at": activity.committed_at,
        "actor_login": activity.actor_login,
        "actor_name": activity.actor_name,
        "actor_kind": activity.actor_kind,
        "co_author_agents": list(activity.co_author_agents),
        "subject": activity.subject,
        "files": [{"path": f.path, "status": f.status} for f in activity.files],
        "files_changed": activity.files_changed,
        "files_truncated": activity.files_truncated,
        "findings": [finding.as_dict() for finding in activity.findings],
        "finding_count": activity.finding_count,
        "finding_rules": list(activity.finding_rules),
        "findings_truncated": activity.findings_truncated,
        "scan_verdict": activity.scan_verdict,
        "future_dated": is_future(activity.committed_at, now),
        "published": False,
        "ingested_at": domain.iso_of(now),
        "ttl": min(domain.epoch_of(activity.committed_at), int(now)) + WINDOW_S,
    }


def is_future(iso: str, now: float) -> bool:
    """True for a time more than SKEW_S past the ingest's clock."""
    return iso > domain.iso_of(now + SKEW_S)


def item_too_large(exc: Exception) -> bool:
    """DynamoDB's refusal of an item over its 400 KB limit."""
    error = (getattr(exc, "response", None) or {}).get("Error") or {}
    return error.get("Code") == "ValidationException" and "size" in str(
        error.get("Message") or ""
    ).lower()


def _conditional_failed(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get("Code") == "ConditionalCheckFailedException"


class ActivityStore:
    def __init__(self, dynamo: Any = None, table: str | None = None) -> None:
        self._dynamo = dynamo
        self.table = table or TABLE

    @property
    def dynamo(self) -> Any:
        return self._dynamo if self._dynamo is not None else client()

    # the repository row

    def get_meta(self, project_repo: str) -> dict[str, Any] | None:
        response = self.dynamo.get_item(
            TableName=self.table,
            Key={"project_repo": project_repo, "sk": META_SK},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return plain(item) if item else None

    def save_meta(self, project_repo: str, fields: dict[str, Any]) -> None:
        names, values, sets = {}, {}, []
        for index, (name, value) in enumerate(sorted(fields.items())):
            names[f"#f{index}"] = name
            values[f":v{index}"] = value
            sets.append(f"#f{index} = :v{index}")
        self.dynamo.update_item(
            TableName=self.table,
            Key={"project_repo": project_repo, "sk": META_SK},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    # commit rows

    def put_activity(self, activity: domain.Activity, now: float) -> str:
        """NEW when this call wrote the row; otherwise the row was already
        there and is UNPUBLISHED or PUBLISHED (its event accepted or not)."""
        try:
            self.dynamo.put_item(
                TableName=self.table,
                Item=item_of(activity, now),
                ConditionExpression="attribute_not_exists(#pk)",
                ExpressionAttributeNames={"#pk": "project_repo"},
            )
            return NEW
        except Exception as exc:
            if not _conditional_failed(exc):
                raise
        response = self.dynamo.get_item(
            TableName=self.table,
            Key={"project_repo": activity.project_repo, "sk": activity.sort_key},
            ConsistentRead=True,
            ProjectionExpression="#p",
            ExpressionAttributeNames={"#p": "published"},
        )
        published = (response.get("Item") or {}).get("published")
        return PUBLISHED if published is True else UNPUBLISHED

    def mark_published(self, activity: domain.Activity) -> None:
        self.dynamo.update_item(
            TableName=self.table,
            Key={"project_repo": activity.project_repo, "sk": activity.sort_key},
            UpdateExpression="SET #p = :t",
            ConditionExpression="attribute_exists(#pk)",
            ExpressionAttributeNames={"#p": "published", "#pk": "project_repo"},
            ExpressionAttributeValues={":t": True},
        )

    def _query(
        self,
        project_repo: str,
        floor: str,
        *,
        newest_first: bool,
        limit: int,
        attributes: Iterable[str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Commit rows of one repository with sk > floor, at most ``limit``;
        the second value is True when more rows were left unread."""
        kwargs: dict[str, Any] = {
            "TableName": self.table,
            "KeyConditionExpression": "#pk = :pk AND #sk > :floor",
            "ExpressionAttributeNames": {"#pk": "project_repo", "#sk": "sk"},
            "ExpressionAttributeValues": {":pk": project_repo, ":floor": floor},
            "ScanIndexForward": not newest_first,
            "Limit": min(limit, 500),
        }
        if attributes is not None:
            names = {f"#a{i}": name for i, name in enumerate(attributes)}
            kwargs["ExpressionAttributeNames"].update(names)
            kwargs["ProjectionExpression"] = ", ".join(names)
        rows: list[dict[str, Any]] = []
        while True:
            response = self.dynamo.query(**kwargs)
            rows.extend(plain(item) for item in response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if len(rows) >= limit:
                return rows[:limit], bool(last) or len(rows) > limit
            if not last:
                return rows, False
            kwargs["ExclusiveStartKey"] = last

    def window_rows(
        self, project_repo: str, now: float, attributes: Iterable[str]
    ) -> tuple[list[dict[str, Any]], bool]:
        return self._query(
            project_repo,
            window_floor(now),
            newest_first=False,
            limit=MAX_WINDOW_ROWS,
            attributes=tuple(attributes),
        )

    def recent(
        self, project_repo: str, now: float, limit: int, attributes: Iterable[str]
    ) -> list[dict[str, Any]]:
        """The newest rows, only the named attributes (never the file list,
        so a page of large commits stays small)."""
        rows, _ = self._query(
            project_repo,
            window_floor(now),
            newest_first=True,
            limit=limit,
            attributes=tuple(attributes),
        )
        return rows

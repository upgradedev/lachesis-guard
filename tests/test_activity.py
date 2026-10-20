"""The Activity context (B7a), offline: registry, attribution, the
deterministic scan, the GitHub client against a fake GitHub (pagination,
rate limits, the cursor), the ingest service against an in-memory
DynamoDB and bus, the private-repository redaction of the anonymous
lane, the API routes and the stack wiring. No network: every GitHub
answer is a fake response.
"""

from __future__ import annotations

import ast
import copy
import email.message
import importlib.util
import io
import json
import re
import sys
import urllib.error
import urllib.parse
from decimal import Decimal
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
LAMBDA_DIR = ROOT / "approvals" / "lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from activity import bus as activity_bus  # noqa: E402
from activity import domain, github, read, registry, scan, service  # noqa: E402
from activity import store as activity_store  # noqa: E402

NOW = domain.epoch_of("2026-10-20T12:00:00Z")
TOKEN = "ghp_" + "T" * 36
ADDRESS = "alice@example.com"


# --- fakes ----------------------------------------------------------------


def _headers(values: dict) -> email.message.Message:
    message = email.message.Message()
    for key, value in values.items():
        message[key] = str(value)
    return message


class FakeResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def sha_of(n: int) -> str:
    return f"{n:040x}"


def make_commit(n, when, *, login="alice", kind="User", name="Alice A", message="Fix a thing",
                files=None):
    return {
        "sha": sha_of(n),
        "commit": {
            "author": {"name": name, "email": ADDRESS, "date": when},
            "committer": {"name": name, "email": ADDRESS, "date": when},
            "message": message,
        },
        "author": {"login": login, "type": kind} if login else None,
        "files": files if files is not None else [
            {"filename": "README.md", "status": "modified", "patch": "@@ -1 +1 @@\n+hello"}
        ],
    }


class FakeGitHub:
    """A tiny GitHub REST API: repos, commit listings with since and pages,
    commit details with paged files, rate-limit headers, injected errors."""

    def __init__(self):
        self.repos = {}
        self.page_size = 2
        self.files_page_size = 300
        self.remaining = 4000
        self.requests = []
        self.fail = {}  # path -> (status, headers)
        self.next_host = "https://api.github.com"

    def add(self, repo, private, commits):
        self.repos[repo] = {"private": private, "commits": commits}

    def __call__(self, request, timeout=None):
        assert timeout is not None
        assert request.get_method() == "GET"
        url = request.full_url
        self.requests.append((url, dict(request.header_items())))
        parts = urllib.parse.urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == "api.github.com"
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.remaining -= 1
        limits = {"X-RateLimit-Remaining": self.remaining, "X-RateLimit-Reset": NOW + 3600}
        if parts.path in self.fail:
            status, extra = self.fail[parts.path]
            raise urllib.error.HTTPError(url, status, "no", _headers({**limits, **extra}),
                                         io.BytesIO(b"{}"))
        match = re.fullmatch(r"/repos/([^/]+)/([^/]+)(/commits(?:/([0-9a-f]{40}))?)?", parts.path)
        assert match, parts.path
        repo = urllib.parse.unquote(match.group(1)) + "/" + urllib.parse.unquote(match.group(2))
        if repo not in self.repos:
            raise urllib.error.HTTPError(url, 404, "nf", _headers(limits), io.BytesIO(b"{}"))
        data = self.repos[repo]
        page = int(query.get("page", "1"))
        if not match.group(3):
            body = {"full_name": repo, "private": data["private"]}
            return FakeResponse(200, _headers(limits), json.dumps(body).encode())
        if not match.group(4):
            since = query["since"]
            listed = [
                {"sha": c["sha"], "commit": c["commit"], "author": c["author"]}
                for c in data["commits"]
                if domain.iso_utc(c["commit"]["committer"]["date"]) >= since
            ]
            chunk, more = self._page(listed, page, self.page_size)
        else:
            commit = next(c for c in data["commits"] if c["sha"] == match.group(4))
            files, more = self._page(commit["files"], page, self.files_page_size)
            chunk = {**{k: v for k, v in commit.items() if k != "files"}, "files": files}
        headers = dict(limits)
        if more:
            query["page"] = str(page + 1)
            next_url = f"{self.next_host}{parts.path}?{urllib.parse.urlencode(query)}"
            headers["Link"] = f'<{next_url}>; rel="next", <{next_url}>; rel="last"'
        return FakeResponse(200, _headers(headers), json.dumps(chunk).encode())

    @staticmethod
    def _page(items, page, size):
        start = (page - 1) * size
        return items[start : start + size], len(items) > start + size


def _as_read(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, dict):
        return {key: _as_read(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_read(item) for item in value]
    return value


def _reject_floats(value):
    if isinstance(value, float):
        raise TypeError("Float types are not supported. Use Decimal types instead.")
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    if isinstance(value, list):
        for item in value:
            _reject_floats(item)


class FakeDynamo:
    """lachesis-activity in memory, the requests the adapter issues."""

    def __init__(self):
        self.items = {}
        self.calls = []
        self.fail_reads = False

    def _key(self, key):
        return (key["project_repo"], key["sk"])

    def get_item(self, TableName, Key, ConsistentRead=False, **kwargs):  # noqa: N803
        assert TableName == "lachesis-activity" and ConsistentRead
        if self.fail_reads:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Get")
        self.calls.append("GetItem")
        item = self.items.get(self._key(Key))
        if item and "ProjectionExpression" in kwargs:
            names = kwargs["ExpressionAttributeNames"]
            item = {names[p.strip()]: item[names[p.strip()]]
                    for p in kwargs["ProjectionExpression"].split(",")
                    if names[p.strip()] in item}
        return {"Item": _as_read(copy.deepcopy(item))} if item else {}

    def put_item(self, TableName, Item, ConditionExpression, ExpressionAttributeNames):  # noqa: N803
        _reject_floats(Item)
        assert ConditionExpression == "attribute_not_exists(#pk)"
        assert ExpressionAttributeNames == {"#pk": "project_repo"}
        self.calls.append("PutItem")
        key = self._key(Item)
        if key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = copy.deepcopy(Item)
        return {}

    def update_item(self, TableName, Key, UpdateExpression, ExpressionAttributeNames,  # noqa: N803
                    ExpressionAttributeValues, ConditionExpression=None):  # noqa: N803
        _reject_floats(ExpressionAttributeValues)
        self.calls.append("UpdateItem")
        key = self._key(Key)
        if ConditionExpression == "attribute_exists(#pk)" and key not in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "Update")
        assert UpdateExpression.startswith("SET ")
        item = self.items.setdefault(key, dict(Key))
        for name, value in re.findall(r"(#\w+) = (:\w+)", UpdateExpression):
            item[ExpressionAttributeNames[name]] = copy.deepcopy(ExpressionAttributeValues[value])
        return {}

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeNames,  # noqa: N803
              ExpressionAttributeValues, ScanIndexForward=True, Limit=None,  # noqa: N803
              ExclusiveStartKey=None, ProjectionExpression=None):  # noqa: N803
        if self.fail_reads:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Q")
        self.calls.append("Query")
        assert KeyConditionExpression == "#pk = :pk AND #sk > :floor"
        pk, floor = ExpressionAttributeValues[":pk"], ExpressionAttributeValues[":floor"]
        rows = sorted(
            (item for (p, sk), item in self.items.items() if p == pk and sk > floor),
            key=lambda item: item["sk"],
            reverse=not ScanIndexForward,
        )
        if ExclusiveStartKey:
            after = ExclusiveStartKey["sk"]
            rows = [r for r in rows if (r["sk"] > after if ScanIndexForward else r["sk"] < after)]
        page = rows[:Limit] if Limit else rows
        if ProjectionExpression:
            keep = [ExpressionAttributeNames[p.strip()] for p in ProjectionExpression.split(",")]
            page = [{k: r[k] for k in keep if k in r} | {"sk": r["sk"]} for r in page]
        response = {"Items": [_as_read(copy.deepcopy(r)) for r in page]}
        if Limit and len(rows) > Limit:
            response["LastEvaluatedKey"] = {"project_repo": pk, "sk": page[-1]["sk"]}
        return response

    def commit_rows(self, project_repo):
        return sorted(
            (item for (p, sk), item in self.items.items() if p == project_repo and sk != "#meta"),
            key=lambda item: item["sk"],
        )

    def meta(self, project_repo):
        return self.items.get((project_repo, "#meta"))


class FakeEvents:
    def __init__(self):
        self.entries = []
        self.refuse = 0  # refuse the next n entries

    def put_events(self, Entries):  # noqa: N803
        assert len(Entries) == 1
        if self.refuse:
            self.refuse -= 1
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalFailure"}]}
        self.entries.extend(copy.deepcopy(Entries))
        return {"FailedEntryCount": 0, "Entries": [{"EventId": f"ev-{len(self.entries)}"}]}

    def details(self):
        return [json.loads(entry["Detail"]) for entry in self.entries]


PROJECT = registry.Project("lachesis", "Lachesis", ("upgradedev/lachesis-guard",))
REPO = "upgradedev/lachesis-guard"
PR = PROJECT.project_repo(REPO)


@pytest.fixture()
def world():
    server = FakeGitHub()
    dynamo = FakeDynamo()
    events = FakeEvents()
    gh = github.GitHub(TOKEN, opener=server)
    store = activity_store.ActivityStore(dynamo=dynamo, table="lachesis-activity")
    bus = activity_bus.Bus(events_client=events)
    return server, dynamo, events, gh, store, bus


def _run(world, project=PROJECT, repo=REPO, now=NOW, **kwargs):
    _, _, _, gh, store, bus = world
    return service.ingest_repo(project, repo, gh=gh, store=store, bus=bus, now=now, **kwargs)


# --- registry ---------------------------------------------------------------


def test_the_registry_names_the_six_real_projects():
    projects = registry.load()
    assert [(p.key, p.name, p.repos) for p in projects] == [
        ("lachesis", "Lachesis", ("upgradedev/lachesis-guard",)),
        ("agon", "Agon", ("upgradedev/agon-azure",)),
        ("elenchos", "Elenchos", ("upgradedev/elenchos-loop", "upgradedev/elenchos")),
        ("volcan", "Volcan", ("upgradedev/volcan_amazon",)),
        ("mercury", "Mercury", ("upgradedev/mercury_amazon",)),
        ("aletheia", "Aletheia", ("upgradedev/aletheia_amazon",)),
    ]
    assert registry.find(projects, "elenchos").name == "Elenchos"
    assert registry.find(projects, "nope") is None


@pytest.mark.parametrize(
    "document",
    [
        {"projects": [{"key": "a", "name": "A", "repos": ["o/r"]},
                      {"key": "a", "name": "B", "repos": ["o/s"]}]},
        {"projects": [{"key": "a", "name": "A", "repos": ["o/r"]},
                      {"key": "b", "name": "B", "repos": ["o/r"]}]},
        {"projects": [{"key": "Bad Key", "name": "A", "repos": ["o/r"]}]},
        {"projects": [{"key": "a", "name": "A", "repos": ["../etc"]}]},
        {"projects": [{"key": "a", "name": "", "repos": ["o/r"]}]},
    ],
)
def test_a_bad_registry_is_refused(document):
    with pytest.raises(ValueError):
        registry.parse(document)


# --- attribution --------------------------------------------------------------


@pytest.mark.parametrize(
    ("trailer", "agent"),
    [
        ("Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", "Claude"),
        ("co-authored-by: claude <x@y>", "Claude"),
        ("Co-authored-by: Codex <codex@openai.com>", "Codex"),
        ("Co-authored-by: OpenAI Codex <bot@openai.com>", "Codex"),
        ("Co-authored-by: Gemini <gemini@google.com>", "Gemini"),
        ("Co-authored-by: Antigravity <agent@example.com>", "Gemini"),
        ("Co-authored-by: Claudette Smith <c@example.com>", None),
        ("Co-authored-by: Bob <claude@anthropic.com>", None),
        ("Co-authored-by: Copilot <copilot@github.com>", None),
    ],
)
def test_trailers_map_exactly(trailer, agent):
    message = "Subject line\n\nBody text.\n\n" + trailer
    assert domain.co_author_agents(message) == ((agent,) if agent else ())


def test_several_trailers_are_listed_once_each_in_a_fixed_order():
    message = (
        "x\n\nCo-Authored-By: Gemini <a@b>\nCo-Authored-By: Claude <c@d>\n"
        "Co-Authored-By: Claude Sonnet <e@f>\nSigned-off-by: Alice <alice@example.com>"
    )
    assert domain.co_author_agents(message) == ("Claude", "Gemini")
    # A trailer word inside the body is not a trailer.
    assert domain.co_author_agents("x\n\nWe asked whether Co-Authored-By matters") == ()


@pytest.mark.parametrize(
    ("author", "name", "agents", "kind"),
    [
        ({"login": "dependabot[bot]", "type": "Bot"}, "dependabot[bot]", (), "bot"),
        ({"login": "renovate", "type": "Bot"}, "Renovate", (), "bot"),
        ({"login": "some-app[bot]", "type": "User"}, "x", (), "bot"),
        (None, "github-actions[bot]", (), "bot"),
        ({"login": "dependabot[bot]", "type": "Bot"}, "d", ("Claude",), "bot"),
        ({"login": "alice", "type": "User"}, "Alice", ("Claude",), "agent-assisted"),
        ({"login": "alice", "type": "User"}, "Alice", (), "human"),
        (None, "Unlinked Person", (), "human"),
    ],
)
def test_actor_kind(author, name, agents, kind):
    assert domain.actor_kind(domain.is_bot(author, name), agents) == kind


def test_build_never_keeps_an_address_or_the_message_body():
    commit = make_commit(
        1,
        "2026-10-19T10:00:00+02:00",
        name="Alice <alice@example.com>",
        message="Ship it, ping alice@example.com\n\nSecret plan.\n\n"
        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
    )
    activity = domain.build("lachesis", REPO, commit, commit["files"])
    assert activity.committed_at == "2026-10-19T08:00:00Z"
    assert activity.actor_kind == "agent-assisted"
    assert activity.co_author_agents == ("Claude",)
    assert activity.actor_login == "alice"
    item = activity_store.item_of(activity, NOW)
    dumped = json.dumps(item)
    assert "@" not in dumped
    assert "Secret plan" not in dumped and "anthropic" not in dumped
    assert item["subject"] == "Ship it, ping [address]"


def test_build_refuses_a_commit_without_a_sha_or_a_date():
    with pytest.raises(ValueError):
        domain.build("p", REPO, {"sha": "xyz"}, [])
    with pytest.raises(ValueError):
        domain.build("p", REPO, {"sha": sha_of(1), "commit": {}}, [])


# --- the deterministic scan ---------------------------------------------------


def _rules(changes):
    return [(f.rule, f.path) for f in scan.scan(changes)]


def test_each_scan_rule_fires_on_its_own_change():
    fc = scan.FileChange
    assert _rules([fc("old.py", "removed")]) == [("file_deleted", "old.py")]
    assert _rules([fc(".github/workflows/ci.yml", "modified")]) == [
        ("workflow_changed", ".github/workflows/ci.yml")
    ]
    for path in ("infra/app.py", "svc/infra/x.yaml", "main.tf", "prod.tfvars", "cdk.json",
                 "deploy/cdk/stack.py"):
        assert _rules([fc(path, "modified")]) == [("infrastructure_changed", path)], path
    for path in ("db/migrations/0001.sql", "alembic/versions/a.py", "db/migrate/1.rb"):
        assert _rules([fc(path, "added")]) == [("database_migration", path)], path
    assert _rules([fc("q.sql", "modified", "@@\n+drop table users;")]) == [
        ("database_migration", "q.sql")
    ]
    assert _rules([fc("a.py", "modified", "@@\n+KEY = 'AKIA" + "Z" * 16 + "'")]) == [
        ("secret_in_added_line", "a.py")
    ]
    assert _rules([fc("README.md", "modified", "@@\n+hello\n-AKIA" + "Z" * 16)]) == []


def test_a_removed_line_or_a_droplet_is_not_a_finding():
    fc = scan.FileChange
    assert _rules([fc("q.sql", "modified", "@@\n-DROP TABLE users;\n+SELECT 1;")]) == []
    assert _rules([fc("notes.md", "modified", "@@\n+we will dropTableau later")]) == []
    assert _rules([fc("a.py", "modified", "@@\n+x = 1")]) == []
    assert scan.verdict([]) == "clean"


@pytest.mark.parametrize("patch", [
    # GitHub's patch has no ---/+++ headers: an added line whose own text
    # starts with ++ is an added line and is scanned.
    '@@ -1 +1 @@\n+++i; const k = "AKIA' + "Q" * 16 + '";',
    # Lines are split on newline only: a form feed, a separator, NEL or a
    # Unicode line separator never starts a line that escapes the scan.
    '@@\n+x = 1;\x0c++ k = "AKIA' + "Q" * 16 + '"',
    '@@\n+x = 1; k = "AKIA' + "Q" * 16 + '"',
    '@@\n+x = 1;\x1ck = "AKIA' + "Q" * 16 + '"',
    '@@\n+x = 1;\x85k = "AKIA' + "Q" * 16 + '"',
])
def test_every_added_line_is_scanned_whatever_it_starts_with(patch):
    assert _rules([scan.FileChange("a.js", "modified", patch)]) == [
        ("secret_in_added_line", "a.js")
    ]


def test_added_text_says_when_the_cap_cut_the_lines():
    short, cut = scan.added_text("@@\n+" + "a" * 10)
    assert (short, cut) == ("a" * 10, False)
    text, cut = scan.added_text("@@\n+" + "a" * (scan.SCAN_CHARS + 5))
    assert cut is True and len(text) == scan.SCAN_CHARS
    exact, cut = scan.added_text("@@\n+" + "a" * scan.SCAN_CHARS)
    assert cut is False and len(exact) == scan.SCAN_CHARS
    _, cut = scan.added_text("@@\n+" + "a" * scan.SCAN_CHARS + "\n+b")
    assert cut is True
    _, cut = scan.added_text("@@\n+" + "a" * scan.SCAN_CHARS + "\n context")
    assert cut is False


def test_a_secret_finding_never_quotes_the_secret():
    secret = "ghp_" + "S" * 36
    [finding] = scan.scan([scan.FileChange("cfg.py", "modified", "@@\n+TOKEN = '" + secret + "'")])
    assert finding.rule == "secret_in_added_line"
    assert secret not in json.dumps(finding.as_dict())
    assert finding.reason == "an added line contains a GitHub token"


def test_one_file_can_raise_several_rules_and_the_label_is_deterministic():
    fc = scan.FileChange
    found = _rules([fc(".github/workflows/deploy.yml", "removed")])
    assert found == [("file_deleted", ".github/workflows/deploy.yml"),
                     ("workflow_changed", ".github/workflows/deploy.yml")]
    assert scan.LABEL == "deterministic scan"
    assert "decider" not in scan.LABEL.lower()


def test_the_scan_reads_a_bounded_amount_of_each_patch():
    patch = "@@\n" + "\n".join("+" + "x" * 99 for _ in range(1000)) + "\n+AKIA" + "Q" * 16
    assert len(scan.added_lines(patch)) <= scan.SCAN_CHARS
    assert scan.scan([scan.FileChange("big.txt", "modified", patch)]) == []


def test_long_key_like_runs_scan_in_bounded_time_and_long_quoted_secrets_are_found():
    import time

    # Quadratic for the copied key-like pattern before the run cap (16.9 s).
    for line in ("a-" * 10000, "my-kebab-case-token-name-" * 800, ("a-" * 128 + ".") * 78):
        started = time.perf_counter()
        assert scan.secret_in_text(line[: scan.SCAN_CHARS]) is None
        assert time.perf_counter() - started < 2.0
    assert scan.bounded("a" * 1000 + " b") == "a" * scan.RUN_CHARS + " b"
    jwt = "eyJ" + "a" * 400 + "." + "b" * 300 + "." + "c" * 100
    assert scan.secret_in_text(f'token = "{jwt}"') == "a long secret assigned to a secret-like name"
    assert scan.secret_in_text("x" * 300 + " ghp_" + "A" * 36) == "a GitHub token"
    patch = "@@\n+" + "a-" * 5000 + "\n+API_KEY = 'AKIA" + "Q" * 16 + "'"
    [finding] = scan.scan([scan.FileChange("cfg.py", "modified", patch)])
    assert finding.rule == "secret_in_added_line"


def test_the_commit_budget_and_the_deadline_bound_one_commit():
    fc = scan.FileChange
    big = "@@\n" + "\n".join("+" + "x" * 199 for _ in range(100))  # 20000 chars added
    files = [fc(f"src/f{i}.py", "modified", big) for i in range(25)]
    files.append(fc(".github/workflows/ci.yml", "modified", big))
    found = scan.scan(files)
    first = scan.SCAN_COMMIT_CHARS // len(scan.added_lines(big))
    assert [f.rule for f in found] == ["scan_incomplete", "workflow_changed"]
    assert found[0].path == f"src/f{first}.py" and found[0].reason == scan.BUDGET_REASON
    calls = []
    with pytest.raises(scan.OutOfTime):
        scan.scan(files, lambda: calls.append(1) or len(calls) > 2)
    assert len(calls) == 3


def test_a_rename_is_checked_by_the_path_it_had():
    fc = scan.FileChange
    moved = fc("docs/ci.yml.bak", "renamed", "", ".github/workflows/ci.yml")
    assert [(f.rule, f.path) for f in scan.scan([moved])] == [
        ("workflow_changed", ".github/workflows/ci.yml")
    ]
    assert "rename" in scan.scan([moved])[0].reason
    out_of_infra = fc("app/stack.py", "renamed", "", "infra/stack.py")
    assert _rules([out_of_infra]) == [("infrastructure_changed", "infra/stack.py")]
    within = fc("infra/b.py", "renamed", "", "infra/a.py")
    assert _rules([within]) == [("infrastructure_changed", "infra/b.py")]
    commit = make_commit(1, "2026-10-19T00:00:00Z", files=[
        {"filename": "docs/ci.yml.bak", "status": "renamed",
         "previous_filename": ".github/workflows/ci.yml"}])
    activity = domain.build("p", REPO, commit, commit["files"])
    assert [f.rule for f in activity.findings] == ["workflow_changed"]


def test_a_secret_in_the_subject_is_never_stored():
    key = "AKIA" + "Q" * 16
    assert domain.subject_of(f"Rotate {key} now\n\nbody") == domain.SECRET_SUBJECT
    assert domain.subject_of("x" * 190 + " ghp_" + "A" * 36) == domain.SECRET_SUBJECT
    assert domain.subject_of("Fix the token refresh") == "Fix the token refresh"
    activity = domain.build("p", REPO, make_commit(1, "2026-10-19T00:00:00Z",
                                                   message=f"Add {key}"), [])
    assert activity.subject == domain.SECRET_SUBJECT and key not in repr(activity)


def _dynamo_size(value) -> int:
    """DynamoDB's item size: UTF-8 names plus values, 3 bytes per list or
    map plus 1 per element, numbers about 21 bytes at most."""
    if isinstance(value, dict):
        return 3 + sum(len(k.encode()) + _dynamo_size(v) + 1 for k, v in value.items())
    if isinstance(value, list):
        return 3 + sum(_dynamo_size(v) + 1 for v in value)
    if isinstance(value, bool) or value is None:
        return 1
    if isinstance(value, int):
        return 21
    return len(str(value).encode("utf-8"))


def test_a_thousand_file_deletion_stays_far_below_the_item_limit():
    wide = "\U0001F600" * 300  # 4 bytes per character
    files = [{"filename": f"infra/{wide}/{i}.ts", "status": "removed"} for i in range(1000)]
    commit = make_commit(1, "2026-10-19T00:00:00Z", files=files,
                         name="\U0001F600" * 300, message="\U0001F600" * 900)
    activity = domain.build("p", REPO, commit, files)
    assert activity.finding_count == 2000 and len(activity.findings) == domain.MAX_FINDINGS
    assert activity.findings_truncated and activity.files_truncated
    assert activity.finding_rules == ("file_deleted", "infrastructure_changed")
    assert all(len(f.path.encode()) <= domain.PATH_BYTES for f in activity.findings)
    item = activity_store.item_of(activity, NOW)
    assert item["finding_count"] == 2000 and item["findings_truncated"] is True
    assert _dynamo_size(item) < 150_000  # DynamoDB refuses an item over 400 KB
    detail = activity_bus.detail_of(activity, False, NOW)
    assert detail["finding_count"] == 2000
    assert detail["finding_rules"] == ["file_deleted", "infrastructure_changed"]
    assert domain.cut_bytes("a\U0001F600", 3) == "a"


# --- the GitHub client ----------------------------------------------------------


def test_listing_follows_every_page_and_sends_only_get_with_the_headers(world):
    server, _, _, gh, _, _ = world
    commits = [make_commit(n, f"2026-10-1{n}T00:00:00Z") for n in range(5, 0, -1)]
    server.add(REPO, False, commits)
    listed = gh.commits_since(REPO, "2026-10-01T00:00:00Z")
    assert [c["sha"] for c in listed] == [c["sha"] for c in commits]
    assert len(server.requests) == 3  # 2 + 2 + 1 per page
    for url, headers in server.requests:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["User-agent"] == github.USER_AGENT
        assert headers["Accept"] == "application/vnd.github+json"
        assert TOKEN not in url
    assert "since=2026-10-01T00%3A00%3A00Z" in server.requests[0][0]
    assert TOKEN not in repr(gh)


def test_a_next_link_to_another_host_is_never_followed(world):
    server, _, _, gh, _, _ = world
    server.add(REPO, False, [make_commit(n, f"2026-10-1{n}T00:00:00Z") for n in range(3, 0, -1)])
    server.next_host = "https://evil.example.com"
    assert len(gh.commits_since(REPO, "2026-10-01T00:00:00Z")) == 2
    assert len(server.requests) == 1
    assert github.next_link('<https://api.github.com.evil.io/x>; rel="next"') is None


def test_a_redirect_is_followed_only_on_the_api_host():
    handler = github._ApiOnlyRedirect()
    request = github.GitHub(TOKEN, opener=lambda *a, **k: None)._request(
        github.API + "/repos/o/r"
    )
    off_host = handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/x")
    assert off_host is None
    same = handler.redirect_request(request, None, 301, "Moved", {},
                                    github.API + "/repositories/1")
    assert same is not None and same.full_url == github.API + "/repositories/1"
    # The default opener carries that handler.
    opener = github.default_opener()
    assert any(isinstance(h, github._ApiOnlyRedirect) for h in opener.__self__.handlers)


def test_the_files_of_a_large_commit_are_paged(world):
    server, _, _, gh, _, _ = world
    files = [{"filename": f"f{i}.py", "status": "added"} for i in range(7)]
    server.add(REPO, False, [make_commit(1, "2026-10-19T00:00:00Z", files=files)])
    server.files_page_size = 3
    commit, got, complete = gh.commit(REPO, sha_of(1))
    assert commit["sha"] == sha_of(1) and complete is True
    assert [f["filename"] for f in got] == [f["filename"] for f in files]
    assert len(server.requests) == 3
    assert github.MAX_FILE_PAGES == 30  # GitHub's 3000 files of one commit


def test_a_commit_with_more_file_pages_than_read_is_not_called_clean(world):
    server, _, _, _, _, _ = world
    files = [{"filename": f"f{i}.py", "status": "added"} for i in range(7)]
    server.add(REPO, False, [make_commit(1, "2026-10-19T00:00:00Z", files=files)])
    server.files_page_size = 3
    gh = github.GitHub(TOKEN, opener=server, max_file_pages=2)
    commit, got, complete = gh.commit(REPO, sha_of(1))
    assert len(got) == 6 and complete is False and len(server.requests) == 2
    activity = domain.build("p", REPO, commit, got, files_complete=complete)
    assert activity.files_truncated is True and activity.scan_verdict == "flagged"
    assert [f.rule for f in activity.findings] == ["scan_incomplete"]
    assert activity.findings[0].reason == domain.FILES_UNREAD_REASON


def test_a_body_that_fails_while_it_is_read_is_a_github_error():
    import http.client

    class Cut(FakeResponse):
        def __init__(self, exc):
            super().__init__(200, _headers({}), b"")
            self.exc = exc

        def read(self):
            raise self.exc

    for exc in (TimeoutError("read timed out"), http.client.IncompleteRead(b"{")):
        gh = github.GitHub(TOKEN, opener=lambda request, timeout=None, e=exc: Cut(e))
        with pytest.raises(github.GitHubError) as caught:
            gh.repo(REPO)
        assert caught.value.status == 0 and TOKEN not in str(caught.value)


def test_rate_limits_stop_without_sending_and_on_403_or_retry_after(world):
    server, _, _, gh, _, _ = world
    server.add(REPO, False, [])
    server.remaining = github.RATE_FLOOR + 1
    gh.repo(REPO)  # remaining is now at the floor
    with pytest.raises(github.RateLimited):
        gh.repo(REPO)
    assert len(server.requests) == 1

    fresh = github.GitHub(TOKEN, opener=server)
    server.remaining = 1
    server.fail["/repos/upgradedev/lachesis-guard"] = (403, {})
    with pytest.raises(github.RateLimited) as caught:
        fresh.repo(REPO)  # 403 with X-RateLimit-Remaining 0
    assert caught.value.reset_at == NOW + 3600
    assert TOKEN not in str(caught.value)

    server.remaining = 4000
    server.fail["/repos/upgradedev/lachesis-guard"] = (429, {"Retry-After": "60"})
    with pytest.raises(github.RateLimited) as caught:
        github.GitHub(TOKEN, opener=server).repo(REPO)
    assert caught.value.retry_after == 60

    server.fail["/repos/upgradedev/lachesis-guard"] = (403, {})
    with pytest.raises(github.GitHubError) as caught:
        github.GitHub(TOKEN, opener=server).repo(REPO)
    assert not isinstance(caught.value, github.RateLimited)
    assert caught.value.status == 403


def test_a_missing_repo_and_an_empty_repo(world):
    server, _, _, gh, _, _ = world
    with pytest.raises(github.NotAccessible):
        gh.repo("upgradedev/nothing-here")
    server.add(REPO, False, [])
    server.fail["/repos/upgradedev/lachesis-guard/commits"] = (409, {})
    assert gh.commits_since(REPO, "2026-10-01T00:00:00Z") == []


def test_transport_failures_carry_no_header():
    def broken(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    gh = github.GitHub(TOKEN, opener=broken)
    with pytest.raises(github.GitHubError) as caught:
        gh.repo(REPO)
    assert caught.value.status == 0 and TOKEN not in str(caught.value)
    with pytest.raises(github.GitHubError):
        gh.get("https://example.com/repos/x")
    with pytest.raises(github.GitHubError):
        gh.commit(REPO, "not-a-sha")


# --- the ingest service -------------------------------------------------------


def _three_commits():
    return [
        make_commit(3, "2026-10-19T09:00:00Z", login="dependabot[bot]", kind="Bot",
                    name="dependabot[bot]"),
        make_commit(2, "2026-10-18T09:00:00Z",
                    message="Add infra\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
                    files=[{"filename": "infra/app.py", "status": "added", "patch": "+x"},
                           {"filename": "old.txt", "status": "removed"}]),
        make_commit(1, "2026-10-17T09:00:00Z"),
    ]


def test_first_run_reads_the_window_writes_each_commit_and_publishes_once(world):
    server, dynamo, events, _, _, _ = world
    server.add(REPO, False, _three_commits())
    result = _run(world)
    assert result.status == "ok" and result.written == 3 and result.published == 3
    since = urllib.parse.parse_qs(urllib.parse.urlsplit(server.requests[1][0]).query)["since"]
    assert since == [domain.iso_of(NOW - activity_store.WINDOW_S)]
    rows = dynamo.commit_rows(PR)
    assert [r["sha"] for r in rows] == [sha_of(1), sha_of(2), sha_of(3)]
    assert [r["actor_kind"] for r in rows] == ["human", "agent-assisted", "bot"]
    assert all(r["published"] is True for r in rows)
    assert rows[1]["scan_verdict"] == "flagged" and rows[1]["finding_count"] == 2
    assert rows[1]["ttl"] == domain.epoch_of("2026-10-18T09:00:00Z") + 30 * 86400
    assert [d["sha"] for d in events.details()] == [sha_of(1), sha_of(2), sha_of(3)]
    meta = dynamo.meta(PR)
    assert meta["cursor"] == "2026-10-19T09:00:00Z" and meta["seen_at_cursor"] == [sha_of(3)]
    assert meta["private"] is False and meta["last_activity_at"] == "2026-10-19T09:00:00Z"
    assert meta["commits"] == 3 and meta["flagged_changes"] == 2
    assert meta["by_actor_kind"] == {"human": 1, "agent-assisted": 1, "bot": 1}
    assert meta["by_scan_verdict"] == {"clean": 2, "flagged": 1}
    assert ADDRESS not in json.dumps([dynamo.items[k] for k in dynamo.items], default=str)


def test_the_cursor_skips_what_was_seen_and_picks_up_new_commits(world):
    server, dynamo, events, _, _, _ = world
    commits = _three_commits()
    server.add(REPO, False, commits)
    _run(world)
    server.requests.clear()
    again = _run(world, now=NOW + 900)
    # The boundary commit comes back (since is inclusive) and is skipped
    # without a commit request; nothing is written or published twice.
    assert again.skipped == 1 and again.written == 0 and again.published == 0
    assert all("/commits/" not in url for url, _ in server.requests)
    assert len(events.entries) == 3
    commits.insert(0, make_commit(4, "2026-10-19T12:00:00Z"))
    third = _run(world, now=NOW + 1800)
    assert third.written == 1 and third.published == 1
    assert dynamo.meta(PR)["cursor"] == "2026-10-19T12:00:00Z"
    assert len(dynamo.commit_rows(PR)) == 4 and len(events.entries) == 4


def test_a_refused_event_is_sent_again_and_the_cursor_waits_for_it(world):
    server, dynamo, events, _, _, _ = world
    server.add(REPO, False, _three_commits())
    _run(world, max_commits=1)  # commit 1 only
    events.refuse = 1  # the second commit's event is refused
    result = _run(world)
    assert result.status == "publish_failed"
    rows = {r["sha"]: r for r in dynamo.commit_rows(PR)}
    assert rows[sha_of(2)]["published"] is False and sha_of(3) not in rows
    assert dynamo.meta(PR)["cursor"] == "2026-10-18T09:00:00Z"
    retry = _run(world)
    assert retry.status == "ok" and retry.written == 1 and retry.published == 2
    assert all(r["published"] for r in dynamo.commit_rows(PR))
    shas = [d["sha"] for d in events.details()]
    assert shas == [sha_of(1), sha_of(2), sha_of(3)]


def test_the_per_run_cap_and_the_deadline_leave_the_rest_for_the_next_run(world):
    server, dynamo, events, _, _, _ = world
    server.add(REPO, False, _three_commits())
    capped = _run(world, max_commits=2)
    assert capped.status == "more" and capped.written == 2
    assert dynamo.meta(PR)["cursor"] == "2026-10-19T09:00:00Z"
    assert dynamo.meta(PR)["seen_at_cursor"] == []
    late = _run(world, out_of_time=lambda: True)
    assert late.status == "deadline" and late.written == 0
    done = _run(world)
    assert done.status == "ok" and done.written == 1
    assert len(events.entries) == 3


def test_a_rate_limit_mid_run_keeps_every_unwritten_commit_ahead_of_the_cursor(world):
    server, dynamo, _, _, _, _ = world
    server.add(REPO, False, _three_commits())
    server.fail["/repos/upgradedev/lachesis-guard/commits/" + sha_of(2)] = (
        429, {"Retry-After": "30"})
    result = _run(world)
    assert result.status == "rate_limited" and result.written == 1
    assert dynamo.meta(PR)["cursor"] == "2026-10-18T09:00:00Z"
    del server.fail["/repos/upgradedev/lachesis-guard/commits/" + sha_of(2)]
    assert _run(world).written == 2


def test_an_inaccessible_repo_is_marked_and_nothing_else_happens(world):
    _, dynamo, events, _, _, _ = world
    result = _run(world)
    assert result.status == "unavailable"
    assert dynamo.meta(PR)["status"] == "unavailable" and "cursor" not in dynamo.meta(PR)
    assert events.entries == []


def test_a_future_dated_commit_never_moves_the_cursor(world):
    server, dynamo, events, _, _, _ = world
    deploy = [{"filename": ".github/workflows/deploy.yml", "status": "removed"}]
    commits = [make_commit(9, "2099-01-01T00:00:00Z"), make_commit(1, "2026-10-17T09:00:00Z")]
    server.add(REPO, False, commits)
    first = _run(world)
    assert first.status == "ok" and first.written == 2
    meta = dynamo.meta(PR)
    assert meta["cursor"] == "2026-10-17T09:00:00Z"
    assert meta["future_dated"] == [f"2099-01-01T00:00:00Z#{sha_of(9)}"]
    assert meta["last_activity_at"] == domain.iso_of(NOW)
    future_row = next(r for r in dynamo.commit_rows(PR) if r["sha"] == sha_of(9))
    assert future_row["future_dated"] is True and future_row["ttl"] == NOW + 30 * 86400
    # Seen at the cursor and remembered as future-dated: no commit request.
    server.requests.clear()
    again = _run(world, now=NOW + 900)
    assert again.skipped == 2 and again.written == 0
    assert all("/commits/" not in url for url, _ in server.requests)
    # A real commit after the future-dated one is still listed and scanned.
    commits.insert(1, make_commit(4, "2026-10-20T11:00:00Z", files=deploy))
    third = _run(world, now=NOW + 1800)
    assert third.written == 1 and third.published == 1
    assert dynamo.meta(PR)["cursor"] == "2026-10-20T11:00:00Z"
    row = next(r for r in dynamo.commit_rows(PR) if r["sha"] == sha_of(4))
    assert row["scan_verdict"] == "flagged" and row["future_dated"] is False
    assert len(events.entries) == 3


def test_a_public_repo_that_becomes_unreadable_fails_closed(world):
    server, dynamo, _, _, store, _ = world
    project = registry.Project("lachesis", "Lachesis", (REPO,))
    server.add(REPO, False, _three_commits())
    _run(world)
    assert len(read.activity_view(project, store, approver=False, now=NOW)["recent"]) == 3
    del server.repos[REPO]  # now a 404: private, or gone
    assert _run(world, now=NOW + 900).status == "unavailable"
    assert dynamo.meta(PR)["private"] is True
    view = read.activity_view(project, store, approver=False, now=NOW + 900)
    assert view["recent"] == [] and view["hidden_repos"] == 1
    assert view["repos"] == [{"repo": "", "visibility": "private"}]
    # Any other refusal leaves the visibility unknown, which is hidden too.
    server.add(REPO, False, _three_commits())
    server.fail["/repos/upgradedev/lachesis-guard"] = (403, {})
    assert _run(world, now=NOW + 1800).status == "github_error"
    assert dynamo.meta(PR)["private"] is None
    assert read.activity_view(project, store, approver=False, now=NOW)["recent"] == []
    # A refused token (401) is hidden too, and it fails the run for the alarm.
    server.fail["/repos/upgradedev/lachesis-guard"] = (401, {})
    refused = _run(world, now=NOW + 2000)
    assert refused.status == "auth_failed" and refused.failed
    assert dynamo.meta(PR)["private"] is None
    # A transport failure keeps the last flag read.
    del server.fail["/repos/upgradedev/lachesis-guard"]
    _run(world, now=NOW + 2700)
    server.fail["/repos/upgradedev/lachesis-guard"] = (502, {})
    _run(world, now=NOW + 3600)
    assert dynamo.meta(PR)["private"] is False


class FailingPuts(FakeDynamo):
    """A FakeDynamo whose PutItem fails for chosen shas."""

    def __init__(self, code, message="boom"):
        super().__init__()
        self.code, self.message, self.shas = code, message, set()

    def put_item(self, TableName, Item, ConditionExpression, ExpressionAttributeNames):  # noqa: N803
        if Item["sha"] in self.shas:
            raise ClientError({"Error": {"Code": self.code, "Message": self.message}}, "PutItem")
        return super().put_item(TableName, Item, ConditionExpression, ExpressionAttributeNames)


def _world_with(dynamo):
    server, events = FakeGitHub(), FakeEvents()
    return (server, dynamo, events, github.GitHub(TOKEN, opener=server),
            activity_store.ActivityStore(dynamo=dynamo, table="lachesis-activity"),
            activity_bus.Bus(events_client=events))


def test_a_repo_that_goes_private_and_fails_mid_run_shows_nothing_new_anonymously():
    dynamo = FailingPuts("ProvisionedThroughputExceededException")
    world = _world_with(dynamo)
    server, _, _, gh, store, bus = world
    project = registry.Project("lachesis", "Lachesis", (REPO,))
    commits = [make_commit(1, "2026-10-17T09:00:00Z", message="Public change")]
    server.add(REPO, False, commits)
    _run(world)
    server.repos[REPO]["private"] = True
    commits[:0] = [make_commit(3, "2026-10-19T09:00:00Z", message="PRIVATE plan three"),
                   make_commit(2, "2026-10-18T09:00:00Z", message="PRIVATE secret roadmap")]
    dynamo.shas = {sha_of(3)}
    result = _run(world, now=NOW + 900)
    assert result.status == "store_error" and result.written == 1 and result.failed
    meta = dynamo.meta(PR)
    assert meta["private"] is True and meta["status"] == "store_error"
    assert meta["cursor"] == "2026-10-19T09:00:00Z"  # commit 3 is still ahead
    anonymous = json.dumps(read.activity_view(project, store, approver=False, now=NOW + 900))
    assert "PRIVATE" not in anonymous and "Public change" not in anonymous
    # An error the service does not expect: the flag was saved first, and
    # ingest_all marks the row and the result.
    dynamo.shas = set()
    server.repos[REPO]["private"] = False
    _run(world, now=NOW + 1800)
    server.repos[REPO]["private"] = True

    def broken_query(**kwargs):
        raise RuntimeError("query failed")

    dynamo.query = broken_query
    [failed] = service.ingest_all((project,), gh=gh, store=store, bus=bus, now=NOW + 2700)
    assert failed.status == "error" and failed.failed
    assert dynamo.meta(PR)["private"] is True and dynamo.meta(PR)["status"] == "error"


def test_a_row_too_large_for_dynamodb_is_skipped_and_the_run_reports_it(capsys):
    dynamo = FailingPuts("ValidationException", "Item size has exceeded the maximum allowed size")
    world = _world_with(dynamo)
    server, _, events, gh, store, bus = world
    server.add(REPO, False, _three_commits())
    dynamo.shas = {sha_of(2)}
    result = _run(world)
    assert result.status == "ok" and result.errors == [sha_of(2)] and result.failed
    assert result.written == 2 and dynamo.meta(PR)["cursor"] == "2026-10-19T09:00:00Z"
    assert [d["sha"] for d in events.details()] == [sha_of(1), sha_of(3)]
    # The Lambda reports the run failed, after its log line, so the alarm fires.
    ingest = _ingest_module()
    server.add(REPO, False, _three_commits() + [make_commit(5, "2026-10-19T10:00:00Z")])
    dynamo.shas = {sha_of(5)}
    with pytest.raises(ingest.IngestFailed) as caught:
        ingest.handler({}, None, deps={"github": gh, "store": store, "bus": bus,
                                        "now": NOW + 900, "projects": (PROJECT,)})
    assert str(caught.value) == "1 repositories failed their ingest"
    assert '"failed_repos": 1' in capsys.readouterr().out
    # Any other write error stops the run with the cursor held.
    other = _world_with(FailingPuts("InternalServerError"))
    other[0].add(REPO, False, _three_commits())
    other[1].shas = {sha_of(2)}
    held = _run(other)
    assert held.status == "store_error" and held.errors == []
    assert other[1].meta(PR)["cursor"] == "2026-10-18T09:00:00Z"


def test_the_deadline_inside_a_commit_scan_leaves_it_for_the_next_run(world):
    server, dynamo, events, _, _, _ = world
    server.add(REPO, False, [make_commit(1, "2026-10-17T09:00:00Z")])
    result = _run(world, out_of_time=lambda: any("/commits/" in url for url, _ in server.requests))
    assert result.status == "deadline" and result.written == 0
    assert dynamo.commit_rows(PR) == [] and events.entries == []
    assert _run(world).written == 1


def test_ingest_all_goes_on_after_a_failing_repo_and_stops_on_a_rate_limit(world, capsys):
    server, _, events, gh, store, bus = world
    project = registry.Project("elenchos", "Elenchos", ("upgradedev/a", "upgradedev/b"))
    server.add("upgradedev/b", True, [make_commit(9, "2026-10-19T00:00:00Z")])
    results = service.ingest_all((project,), gh=gh, store=store, bus=bus, now=NOW)
    assert [r.status for r in results] == ["unavailable", "ok"]
    assert len(events.entries) == 1
    server.fail["/repos/upgradedev/a"] = (429, {"Retry-After": "5"})
    stopped = service.ingest_all((project, PROJECT), gh=gh, store=store, bus=bus, now=NOW)
    assert [r.status for r in stopped] == ["rate_limited"]
    assert TOKEN not in capsys.readouterr().out


# --- the bus ------------------------------------------------------------------


def test_activity_observed_detail_is_a_whitelist_and_private_drops_the_login():
    commit = make_commit(
        1, "2026-10-19T00:00:00Z",
        message="Secret subject\n\nCo-Authored-By: Codex <c@openai.com>",
        files=[{"filename": "secret/path.tf", "status": "added", "patch": "+x"}],
    )
    activity = domain.build("volcan", "upgradedev/volcan_amazon", commit, commit["files"])
    private = activity_bus.detail_of(activity, True, NOW)
    public = activity_bus.detail_of(activity, False, NOW)
    assert set(public) == set(activity_bus.DETAIL_FIELDS)
    assert set(private) == set(activity_bus.DETAIL_FIELDS) - activity_bus.PUBLIC_ONLY_FIELDS
    assert public["actor_login"] == "alice" and "actor_login" not in private
    for detail in (private, public):
        dumped = json.dumps(detail)
        assert "Secret subject" not in dumped and "secret/path.tf" not in dumped
        assert "@" not in dumped and "Alice A" not in dumped
        assert detail["type"] == "ActivityObserved"
        assert detail["event_id"] == f"volcan#upgradedev/volcan_amazon#{sha_of(1)}"
        assert detail["finding_rules"] == ["infrastructure_changed"]
        assert detail["scan_label"] == "deterministic scan"
    entry = activity_bus.entry_of(private)
    assert entry["Source"] == "lachesis.activity" and entry["DetailType"] == "ActivityObserved"


def test_a_put_events_that_raises_is_not_accepted():
    class Raising:
        def put_events(self, Entries):  # noqa: N803
            raise RuntimeError("boom")

    activity = domain.build("p", REPO, make_commit(1, "2026-10-19T00:00:00Z"), [])
    detail = activity_bus.detail_of(activity, False, NOW)
    assert activity_bus.Bus(Raising()).publish(detail) is False


# --- the read model: two lanes --------------------------------------------------


@pytest.fixture()
def mixed(world):
    """Elenchos with one public and one private repository, ingested."""
    server, dynamo, _, gh, store, bus = world
    project = registry.Project("elenchos", "Elenchos",
                               ("upgradedev/elenchos-loop", "upgradedev/elenchos"))
    server.add("upgradedev/elenchos-loop", False, [
        make_commit(1, "2026-10-18T00:00:00Z", login="pub-dev", name="Pub Dev",
                    message="Public change",
                    files=[{"filename": "infra/x.tf", "status": "modified", "patch": "+a"}]),
    ])
    server.add("upgradedev/elenchos", True, [
        make_commit(2, "2026-10-19T00:00:00Z", login="priv-dev", name="Priv Dev",
                    message="Private change\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
                    files=[{"filename": "secret/plan.md", "status": "removed"}]),
    ])
    service.ingest_all((project,), gh=gh, store=store, bus=bus, now=NOW)
    return project, store


def test_the_anonymous_lane_shows_a_private_repo_only_as_counts(mixed):
    project, store = mixed
    view = read.projects_view((project,), store, approver=False)
    [summary] = view["projects"]
    assert view["scan_label"] == "deterministic scan" and view["window_days"] == 30
    assert summary["repos"] == [
        {"repo": "upgradedev/elenchos-loop", "visibility": "public",
         "last_activity_at": "2026-10-18T00:00:00Z", "ingest_status": "ok"},
        {"repo": "", "visibility": "private"},
    ]
    assert summary["visibility"] == "mixed"
    assert summary["last_activity_at"] == "2026-10-18T00:00:00Z"
    # Counts and the verdict mix include the private repository.
    assert summary["commits"] == 2
    assert summary["by_actor_kind"] == {"human": 1, "agent-assisted": 1, "bot": 0}
    assert summary["by_scan_verdict"] == {"clean": 0, "flagged": 2}
    assert summary["flagged_changes"] == 2
    activity = read.activity_view(project, store, approver=False, now=NOW)
    assert activity["hidden_repos"] == 1
    assert [c["sha"] for c in activity["recent"]] == [sha_of(1)]
    dumped = json.dumps({"summary": view, "activity": activity})
    for leaked in ("upgradedev/elenchos\"", "priv-dev", "Priv Dev", "Private change",
                   "secret/plan.md", sha_of(2), "2026-10-19T00:00:00Z", "@"):
        assert leaked not in dumped, leaked


def test_the_approver_lane_shows_every_repository(mixed):
    project, store = mixed
    [summary] = read.projects_view((project,), store, approver=True)["projects"]
    assert [r["repo"] for r in summary["repos"]] == list(project.repos)
    assert summary["last_activity_at"] == "2026-10-19T00:00:00Z"
    activity = read.activity_view(project, store, approver=True, now=NOW)
    assert activity["hidden_repos"] == 0
    newest = activity["recent"][0]
    assert newest["sha"] == sha_of(2) and newest["actor_login"] == "priv-dev"
    assert newest["co_author_agents"] == ["Claude"] and newest["subject"] == "Private change"
    assert newest["findings"] == [
        {"rule": "file_deleted", "path": "secret/plan.md", "reason": "the commit deletes this file"}
    ]
    assert newest["finding_count"] == 1 and newest["findings_truncated"] is False
    # The file list is never read back for a page: files_changed is the count.
    assert "files" not in newest and newest["files_changed"] == 1
    assert newest["future_dated"] is False
    assert "@" not in json.dumps(activity)


def test_the_anonymous_lane_never_points_at_a_secret_and_caps_the_findings(world):
    server, _, _, _, store, _ = world
    project = registry.Project("lachesis", "Lachesis", (REPO,))
    leak = [{"filename": "deploy/keys.env", "status": "added",
             "patch": "@@\n+AWS = 'AKIA" + "Q" * 16 + "'"}]
    many = [{"filename": f"old/{i}.txt", "status": "removed"} for i in range(30)]
    server.add(REPO, False, [make_commit(2, "2026-10-19T00:00:00Z", files=many),
                             make_commit(1, "2026-10-18T00:00:00Z", files=leak)])
    _run(world)
    public = read.activity_view(project, store, approver=False, now=NOW)
    by_sha = {c["sha"]: c for c in public["recent"]}
    assert by_sha[sha_of(1)]["findings"] == [
        {"rule": "secret_in_added_line", "path": "", "reason": read.SECRET_REASON_PUBLIC}
    ]
    assert "keys.env" not in json.dumps(public) and "access key" not in json.dumps(public)
    assert len(by_sha[sha_of(2)]["findings"]) == read.FINDINGS_SHOWN
    assert by_sha[sha_of(2)]["finding_count"] == 30
    assert by_sha[sha_of(2)]["findings_truncated"] is True
    approver = read.activity_view(project, store, approver=True, now=NOW)
    [secret] = next(c for c in approver["recent"] if c["sha"] == sha_of(1))["findings"]
    assert secret["path"] == "deploy/keys.env" and "AWS access key id" in secret["reason"]


def test_a_repo_with_no_stored_flag_is_private_and_a_repo_that_goes_public_shows(mixed, world):
    project, store = mixed
    server, dynamo = world[0], world[1]
    dynamo.items[(project.project_repo("upgradedev/elenchos"), "#meta")].pop("private")
    [summary] = read.projects_view((project,), store, approver=False)["projects"]
    assert summary["repos"][1] == {"repo": "", "visibility": "unknown"}
    server.repos["upgradedev/elenchos"]["private"] = False
    service.ingest_all((project,), gh=world[3], store=store, bus=world[5], now=NOW + 900)
    activity = read.activity_view(project, store, approver=False, now=NOW + 900)
    assert {c["sha"] for c in activity["recent"]} == {sha_of(1), sha_of(2)}


def test_an_unknown_project_before_any_ingest_reads_as_empty(world):
    store = world[4]
    [summary] = read.projects_view((PROJECT,), store, approver=False)["projects"]
    assert summary["commits"] == 0 and summary["counted_at"] == ""
    assert summary["repos"] == [{"repo": "", "visibility": "unknown"}]
    assert summary["visibility"] == "unknown"


# --- the ingest Lambda ----------------------------------------------------------


def _ingest_module():
    spec = importlib.util.spec_from_file_location("ingest", LAMBDA_DIR / "ingest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_token_is_read_from_the_secret_and_never_printed(world, capsys):
    ingest = _ingest_module()
    assert ingest.token_from(TOKEN) == TOKEN
    assert ingest.token_from(json.dumps({"token": TOKEN})) == TOKEN
    for bad in ("", "{not json", json.dumps({"other": 1}), "two words"):
        with pytest.raises(ingest.NoToken) as caught:
            ingest.token_from(bad)
        assert TOKEN not in str(caught.value)

    class Secrets:
        def __init__(self):
            self.asked = []

        def get_secret_value(self, SecretId):  # noqa: N803
            self.asked.append(SecretId)
            return {"SecretString": TOKEN}

    secrets = Secrets()
    assert ingest.read_token(secrets) == TOKEN
    assert secrets.asked == ["lachesis/github-readonly"]

    server, _, events, gh, store, bus = world
    server.add(REPO, False, _three_commits())

    class Context:
        def get_remaining_time_in_millis(self):
            return 600_000

    out = ingest.handler({}, Context(), deps={"github": gh, "store": store, "bus": bus,
                                               "now": NOW, "projects": (PROJECT,)})
    assert out["written"] == 3 and out["published"] == 3 and out["statuses"] == ["ok"]
    assert TOKEN not in capsys.readouterr().out


# --- the API routes ---------------------------------------------------------------


@pytest.fixture()
def api(monkeypatch, mixed):
    project, store = mixed
    spec = importlib.util.spec_from_file_location("common", LAMBDA_DIR / "common.py")
    common = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "common", common)
    spec.loader.exec_module(common)
    for name in [n for n in sys.modules if n == "approval" or n.startswith("approval.")]:
        monkeypatch.delitem(sys.modules, name)
    if "ledger" in sys.modules:
        monkeypatch.delitem(sys.modules, "ledger")
    spec = importlib.util.spec_from_file_location("api", LAMBDA_DIR / "api.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "api", module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_activity_store", lambda: store)
    monkeypatch.setattr(module.activity_registry, "load", lambda: (project,))
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    return module


def _call(api, route, key=None, approver=False):
    event = {"routeKey": route, "pathParameters": {"key": key} if key else None}
    if approver:
        event["requestContext"] = {
            "authorizer": {"jwt": {"claims": {"cognito:groups": "[approvers]", "sub": "s"}}}
        }
    out = api.handler(event, None)
    return out["statusCode"], json.loads(out["body"])


def test_the_project_routes_answer_per_lane(api):
    status, body = _call(api, "GET /projects")
    assert status == 200 and body["lane"] == "public"
    assert body["projects"][0]["repos"][1] == {"repo": "", "visibility": "private"}
    status, body = _call(api, "GET /projects/{key}/activity", "elenchos")
    assert status == 200 and [c["sha"] for c in body["recent"]] == [sha_of(1)]
    assert _call(api, "GET /projects/{key}/activity", "nope")[0] == 404
    assert _call(api, "GET /projects/{key}/activity", "../x")[0] == 404
    # The approver routes need the approvers group.
    assert _call(api, "GET /approvals/projects")[0] == 403
    assert _call(api, "GET /approvals/projects/{key}/activity", "elenchos")[0] == 403
    status, body = _call(api, "GET /approvals/projects/{key}/activity", "elenchos", True)
    assert status == 200 and body["lane"] == "approver"
    assert {c["sha"] for c in body["recent"]} == {sha_of(1), sha_of(2)}
    status, body = _call(api, "GET /approvals/projects", approver=True)
    assert status == 200 and body["projects"][0]["repos"][1]["repo"] == "upgradedev/elenchos"


def test_a_failed_read_is_503_and_names_nothing(api, world, capsys):
    world[1].fail_reads = True
    assert _call(api, "GET /projects") == (503, {"error": "the project activity could not be read"})
    assert _call(api, "GET /projects/{key}/activity", "elenchos")[0] == 503
    assert "ClientError" in capsys.readouterr().out


# --- the stack wiring (no CDK here: read as source) --------------------------------


STACK = ROOT / "infra" / "stacks" / "approvals.py"


def test_the_stack_grants_exactly_what_the_ingest_and_the_api_issue():
    source = STACK.read_text(encoding="utf-8")
    grants = re.findall(r"activity_table\.grant\(\s*(\w+),([^)]*)\)", source)
    actions = {fn: sorted(re.findall(r'"(dynamodb:\w+)"', rest)) for fn, rest in grants}
    assert actions == {
        "ingest": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query",
                   "dynamodb:UpdateItem"],
        "api_fn": ["dynamodb:GetItem", "dynamodb:Query"],
    }
    assert "activity_table.grant_" not in source
    assert (
        'f"arn:aws:secretsmanager:eu-west-1:{self.account}:secret:{GITHUB_SECRET_NAME}-??????"'
        in source
    )
    # No async retry of a failed run: the next scheduled run is the retry,
    # so a timed-out run never overlaps the next one.
    ingest = source[source.index('"Ingest",'):source.index('"IngestSchedule"')]
    assert "retry_attempts=0," in ingest
    assert "never overlap" not in ingest
    assert 'actions=["secretsmanager:GetSecretValue"], resources=[github_secret_arn]' in source
    # The ingest and the merge gate (B7b) read the secret, by that ARN only.
    assert source.count("secretsmanager:GetSecretValue") == 2
    assert source.count("resources=[github_secret_arn]") == 2
    assert '{"StringEquals": {"events:source": [ACTIVITY_SOURCE]}}' in source
    assert "events.Schedule.rate(cdk.Duration.minutes(INGEST_RATE_MINUTES))" in source
    assert 'time_to_live_attribute="ttl"' in source[source.index('"ActivityTable"'):]
    constants = {
        node.targets[0].id: node.value.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
    }
    assert constants["ACTIVITY_SOURCE"] == activity_bus.SOURCE
    assert constants["ACTIVITY_TABLE_NAME"] == activity_store.TABLE == "lachesis-activity"
    assert constants["GITHUB_SECRET_NAME"] == "lachesis/github-readonly"
    assert constants["INGEST_RATE_MINUTES"] == 15


def test_the_audit_rule_takes_both_sources():
    sys.path.insert(0, str(ROOT / "infra"))
    from stacks import audit_schema

    audit = (ROOT / "infra" / "stacks" / "audit.py").read_text(encoding="utf-8")
    assert 'event_pattern={"source": [schema.EVENT_SOURCE, schema.ACTIVITY_SOURCE]}' in audit
    assert audit_schema.ACTIVITY_SOURCE == activity_bus.SOURCE
    assert audit_schema.EVENT_SOURCE == "lachesis.approvals"


# --- the console ----------------------------------------------------------------


CONSOLE = ROOT / "approvals" / "console"


def test_the_console_projects_view_renders_only_backend_numbers():
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")
    js = (CONSOLE / "app.js").read_text(encoding="utf-8")
    assert 'data-nav="projects" href="#/projects"' in html
    start = html.index('<section data-view="projects"')
    view = html[start:html.index("\n    </section>", start)]
    for needle in ('id="projects-body"', 'id="project-activity"', "deterministic scan",
                   "never from Decider", "shows only its counts until it is public",
                   "a commit without one counts as human, even when an agent wrote it",
                   'id="projects-private"', "do not record or share it"):
        assert needle in view, needle
    assert "by a person, a person with a coding agent" not in view
    block = js[js.index("function viewProjects(key)"):js.index("function viewDetail(id)")]
    assert "innerHTML" not in block and "insertAdjacentHTML" not in block
    # The anonymous lane is the default, also when signed in; the approver
    # lane is an explicit choice and falls back on a 401 or a 403.
    assert "want = !!auth.token && projPriv" in block
    assert 'api((want ? "/approvals/projects" : "/projects") + path)' in block
    assert '(out.status === 401 || out.status === 403) ? api("/projects" + path)' in block
    assert 'get("/" + encodeURIComponent(key) + "/activity")' in block
    assert "Do not record or share this screen." in block
    assert 'let projPriv = false;' in js and "projPriv = e.target.checked; route();" in js
    # The tile number and the commit mix are named apart.
    assert 'p.flagged_changes + " flagged changes"' in block
    assert '"Commits by deterministic scan: "' in block and '"Actors (by trailer): "' in block
    # A secret finding on the anonymous lane has no path.
    assert '(f.path ? f.path + ": " : "") + f.reason' in block
    # Counts are the backend's: nothing is counted or scored in the page.
    assert "length +" not in block and "reduce(" not in block
    assert "confidence" not in block and "meter(" not in block
    for field in ("p.flagged_changes", "p.commits", "p.by_actor_kind", "p.by_scan_verdict",
                  "b.window_days", "b.hidden_repos", "b.scan_label", "c.files_changed",
                  "c.co_author_agents", "f.reason", "c.finding_count", "c.future_dated"):
        assert field in block, field
    assert 'p[0] === "projects" ? "projects"' in js
    assert 'else if (view === "projects") viewProjects(p[1]);' in js
    # The queue groups by project; a run without one is the demo's.
    queue = js[js.index("function viewQueue()"):js.index("function viewProjects(key)")]
    assert '"Project: " + grp' in queue and 'v.project || "Demo"' in queue

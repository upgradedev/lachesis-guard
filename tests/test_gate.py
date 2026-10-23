"""The merge gate (B7b), offline: GitHub OIDC claim checks in the Lambda,
the gate's own read of the pull request from GitHub, the deterministic
scan to the gate status, the cold-GPU path that never invokes Decider,
the caps, lane isolation, and a person's decision on a gate run.

DynamoDB is the in-memory fake of tests/test_approvals.py; GitHub and
SageMaker are fakes that record every call. No network.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from test_approvals import APPROVER, LAMBDA_DIR, PLAIN_USER, _event, modules  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
REPO = "upgradedev/volcan_amazon"
OTHER_REPO = "upgradedev/mercury_amazon"
SHA = "a" * 40
GITHUB = "https://token.actions.githubusercontent.com"
OWNER_ID = "25751981"
WORKFLOW_FILE = ".github/workflows/lachesis-gate.yml"
AKIA = "AKIA" + "Q" * 16


def _claims(repository=REPO, iss=GITHUB, aud="lachesis-gate", event="pull_request",
            ref="refs/pull/7/merge", owner_id=OWNER_ID, workflow=None):
    workflow = workflow if workflow is not None else f"{repository}/{WORKFLOW_FILE}@{ref}"
    return {"iss": iss, "aud": aud, "repository": repository, "event_name": event, "ref": ref,
            "repository_owner_id": owner_id, "job_workflow_ref": workflow,
            "workflow_ref": workflow, "sub": f"repo:{repository}:pull_request"}


def _change(path="src/app.py", status="modified", excerpt="print('hello')", **extra):
    entry = {"path": path, "status": status, "additions": max(1, len(excerpt.splitlines())),
             "deletions": 0, "added_lines_excerpt": excerpt}
    entry.update(extra)
    return entry


def _body(changes=None, repository=REPO, sha=SHA, **extra):
    body = {"repository": repository, "pr_number": 7, "head_sha": sha,
            "changes": [_change()] if changes is None else changes}
    body.update(extra)
    return body


def _github_file(change):
    """A test change as GET /pulls/{n}/files lists it: a patch of hunks
    only, no ---/+++ headers, the way GitHub sends it."""
    entry = {"filename": change["path"], "status": change["status"],
             "additions": change.get("additions", 0), "deletions": change.get("deletions", 0)}
    excerpt = change.get("added_lines_excerpt") or ""
    if excerpt and not change.get("no_patch"):
        entry["patch"] = "@@ -1,1 +1,1 @@\n" + "\n".join("+" + line for line in excerpt.split("\n"))
    if change.get("previous_path"):
        entry["previous_filename"] = change["previous_path"]
    return entry


class FakeGitHub:
    """GitHub's pull request and its files, per (repository, number)."""

    def __init__(self):
        self.pulls = {}
        self.calls = []
        self.fail = None
        self.head_after_files = None
        self.base_after_files = None

    def put(self, repository, number, head, files, complete=True, state="open",
            changed_files=None, base="main"):
        pull = {"state": state, "head": {"sha": head},
                "changed_files": len(files) if changed_files is None else changed_files}
        if base is not None:
            pull["base"] = {"ref": base}
        self.pulls[(repository, number)] = {"pull": pull, "files": files, "complete": complete}

    def pull(self, repository, number):
        self.calls.append(("pull", repository, number))
        if self.fail:
            raise self.fail
        return json.loads(json.dumps(self.pulls[(repository, number)]["pull"]))

    def pull_files(self, repository, number, max_pages):
        self.calls.append(("files", repository, number, max_pages))
        data = self.pulls[(repository, number)]
        if self.head_after_files:
            data["pull"]["head"]["sha"] = self.head_after_files
        if self.base_after_files:
            data["pull"]["base"] = {"ref": self.base_after_files}
        return json.loads(json.dumps(data["files"])), data["complete"]


class FakeSageMaker:
    """DescribeInferenceComponent and InvokeEndpoint, recorded."""

    def __init__(self, copies=0, status="InService", answer=None, describe_error=None,
                 desired=None):
        self.copies, self.status, self.desired = copies, status, desired
        self.answer = answer or {"choice": "destructive_write", "confidence": 0.93}
        self.describe_error = describe_error
        self.described, self.invoked = [], []

    def describe_inference_component(self, **kwargs):
        self.described.append(kwargs)
        if self.describe_error:
            raise self.describe_error
        desired = self.copies if self.desired is None else self.desired
        return {"InferenceComponentStatus": self.status,
                "RuntimeConfig": {"DesiredCopyCount": desired, "CurrentCopyCount": self.copies}}

    def invoke_endpoint(self, **kwargs):
        self.invoked.append(kwargs)
        payload = {"answers": {"risk_class": self.answer}, "latency_ms": 120.4}
        return {"Body": io.BytesIO(json.dumps(payload).encode())}


@pytest.fixture()
def gate(modules, monkeypatch):  # noqa: F811
    common, api, runner, table = modules
    for name in [n for n in sys.modules if n in {"gate", "gate_api"} or n.startswith("gate.")]:
        monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location("gate_api", LAMBDA_DIR / "gate_api.py")
    gate_api = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "gate_api", gate_api)
    spec.loader.exec_module(gate_api)
    assert gate_api.common is common
    sagemaker, github = FakeSageMaker(), FakeGitHub()
    monkeypatch.setattr(gate_api.decider, "_clients", lambda: (sagemaker, sagemaker))
    monkeypatch.setattr(gate_api, "_github", lambda: github)
    agent_calls, runner_calls = [], []
    monkeypatch.setattr(common, "invoke_agent", lambda *a, **k: agent_calls.append(a) or {})
    monkeypatch.setattr(api, "_invoke_runner", lambda *a, **k: runner_calls.append(a) or {})
    monkeypatch.setattr(runner, "_invoke_runner", lambda payload: runner_calls.append(payload))

    class World:
        pass

    world = World()
    world.common, world.api, world.runner, world.table = common, api, runner, table
    world.gate_api, world.sagemaker, world.github = gate_api, sagemaker, github
    world.agent_calls, world.runner_calls = agent_calls, runner_calls
    return world


def _context(claims):
    return {"authorizer": {"jwt": {"claims": _claims() if claims is None else claims}}}


def _post(world, body, claims=None, raw=None, b64=False, lambda_context=None):
    """POST /gate/pulls. A dict body's ``changes`` (and files_truncated)
    become what the fake GitHub lists for that pull request; the POST
    itself carries only the repository, the number and the head."""
    if isinstance(body, dict) and isinstance(body.get("changes"), list):
        files = [_github_file(change) for change in body["changes"]]
        if isinstance(body.get("pr_number"), int) and isinstance(body.get("head_sha"), str):
            world.github.put(body.get("repository"), body["pr_number"], body["head_sha"], files,
                             complete=not body.get("files_truncated"))
        body = {key: value for key, value in body.items()
                if key not in {"changes", "files_truncated"}}
    text = raw if raw is not None else json.dumps(body)
    event = {"routeKey": "POST /gate/pulls", "body": text, "requestContext": _context(claims)}
    if b64:
        event["body"], event["isBase64Encoded"] = base64.b64encode(text.encode()).decode(), True
    out = world.gate_api.handler(event, lambda_context)
    return out["statusCode"], json.loads(out["body"])


def _get(world, gate_id, claims=None):
    event = {"routeKey": "GET /gate/{id}", "pathParameters": {"id": gate_id},
             "requestContext": _context(claims)}
    out = world.gate_api.handler(event, None)
    return out["statusCode"], json.loads(out["body"])


def _row(world, gate_id):
    return world.table.items[gate_id]


def _card(world, view):
    return json.loads(_row(world, view["gate_id"])["card"])


def _event_types(world, gate_id):
    return [row["event_type"] for row in world.table.dynamo.events_of(gate_id)]


def _gate_rows(world):
    return {key: row for key, row in world.table.items.items() if str(key).startswith("appr-")}


def _post_counter_prefix(world, repository=REPO):
    return f"gate-post-{world.gate_api.service._digest(repository)}-"


def _written(world, repository=REPO):
    """Every row on lachesis-approvals except the repository's hourly
    POST counter, which each accepted POST takes before it reads GitHub
    (so a refused read still counts toward the cap)."""
    prefix = _post_counter_prefix(world, repository)
    return {key: row for key, row in world.table.items.items()
            if not str(key).startswith(prefix)}


# --- the authorizer's claims, checked again in the Lambda -------------------------


@pytest.mark.parametrize(
    "claims",
    [
        {},
        _claims(iss="https://cognito-idp.eu-west-1.amazonaws.com/pool"),
        _claims(aud="sts.amazonaws.com"),
        _claims(repository="someone/else"),
        _claims(repository="upgradedev/lachesis-guard/../x"),
        # The registered name, but not the pinned owner account: a name
        # taken again after a rename or a deletion gets nothing.
        _claims(owner_id="999"),
        {key: value for key, value in _claims().items() if key != "repository_owner_id"},
        {"cognito:groups": "approvers", "sub": "s"},
    ],
)
def test_only_a_github_token_of_a_registered_repository_gets_in(gate, claims):
    assert _post(gate, _body(), claims)[0] == 403
    assert _get(gate, "appr-gate-" + "0" * 40 + "-0", claims)[0] == 403
    assert gate.table.items == {} and gate.sagemaker.described == []
    assert gate.github.calls == []


def test_every_registered_repository_is_accepted_and_an_array_audience_too(gate):
    registry = json.loads((LAMBDA_DIR / "projects.json").read_text(encoding="utf-8"))
    for project in registry["projects"]:
        for repo in project["repos"]:
            claims = _claims(repository=repo, aud="[lachesis-gate]")
            status, view = _post(gate, _body(repository=repo), claims)
            assert status == 202 and view["status"] == "PASSED", repo
            assert _row(gate, view["gate_id"])["project"] == project["name"]
            assert repo.split("/")[0] in gate.gate_api.OWNER_IDS


@pytest.mark.parametrize(
    "claims",
    [
        _claims(event="push", ref="refs/heads/main"),
        _claims(event="workflow_dispatch", ref="refs/pull/7/merge"),
        _claims(ref="refs/heads/feature"),
        {key: value for key, value in _claims().items() if key != "ref"},
        # Another workflow file of the pull request itself, with id-token:
        # write, cannot open or pre-empt the gate run.
        _claims(workflow=f"{REPO}/.github/workflows/other.yml@refs/pull/7/merge"),
        {**_claims(), "workflow_ref": f"{REPO}/.github/workflows/caller.yml@refs/pull/7/merge"},
        {key: value for key, value in _claims().items() if key != "job_workflow_ref"},
        _claims(workflow=f"{REPO}/{WORKFLOW_FILE}@refs/heads/main"),
    ],
)
def test_only_this_repositorys_gate_workflow_of_a_pull_request_may_post(gate, claims):
    status, body = _post(gate, _body([]), claims)
    assert status == 403 and "gate run of a pull request" in body["error"]
    assert gate.table.items == {} and gate.github.calls == []
    assert _post(gate, _body())[0] == 202


def test_the_body_must_name_the_tokens_own_repository_and_pull_request(gate):
    status, body = _post(gate, _body(repository=OTHER_REPO))
    assert status == 403 and "another repository" in body["error"]
    status, body = _post(gate, _body(), _claims(ref="refs/pull/8/merge"))
    assert status == 403 and "not from a run of this pull request" in body["error"]
    assert gate.table.items == {}


# --- the request contract ------------------------------------------------------


def test_the_body_is_capped_and_a_list_of_changes_in_it_is_ignored(gate):
    policy = gate.gate_api.policy
    assert policy.MAX_BODY_BYTES <= 4096
    big = {"repository": REPO, "pr_number": 7, "head_sha": SHA, "pad": "x" * 5000}
    assert _post(gate, big)[0] == 413
    # GitHub lists a workflow change; the body claims nothing changed.
    gate.github.put(REPO, 7, SHA, [_github_file(_change(".github/workflows/ci.yml"))])
    status, view = _post(gate, None, raw=json.dumps({**_body(), "changes": []}))
    assert status == 202 and view["status"] == "PENDING"
    assert view["summary"]["files_by_rule"] == {"workflow_changed": 1}


@pytest.mark.parametrize(
    "body",
    [
        {**_body(), "head_sha": "ABC"},
        {**_body(), "head_sha": "A" * 40},
        {**_body(), "pr_number": True},
        {**_body(), "pr_number": "7"},
        {**_body(), "repository": "nope"},
    ],
)
def test_a_malformed_body_is_400_and_writes_nothing(gate, body):
    assert _post(gate, None, raw=json.dumps(body))[0] == 400
    assert gate.table.items == {}


def test_a_body_that_is_not_json_or_too_deep_is_400_and_base64_bodies_are_read(gate):
    assert _post(gate, None, raw="{not json")[0] == 400
    assert _post(gate, None, raw="[]")[0] == 400
    assert _post(gate, None, raw="[" * 1500 + "]" * 1500)[0] == 400
    status, view = _post(gate, _body(), b64=True)
    assert status == 202 and view["status"] == "PASSED"


# --- GitHub, read by the gate itself ---------------------------------------------


def test_the_gate_scans_what_github_lists_at_githubs_head(gate):
    status, view = _post(gate, _body([_change("infra/main.tf")]))
    assert status == 202
    assert gate.github.calls == [("pull", REPO, 7), ("files", REPO, 7, 3), ("pull", REPO, 7)]
    assert _row(gate, view["gate_id"])["head_sha"] == SHA


@pytest.mark.parametrize("state", ["closed"])
def test_a_pull_request_that_is_not_open_is_409(gate, state):
    gate.github.put(REPO, 7, SHA, [_github_file(_change())], state=state)
    status, body = _post(gate, None, raw=json.dumps({"repository": REPO, "pr_number": 7,
                                                     "head_sha": SHA}))
    assert status == 409 and "not open" in body["error"] and _written(gate) == {}


def test_a_head_that_moved_is_409_and_records_nothing(gate):
    gate.github.put(REPO, 7, "b" * 40, [_github_file(_change())])
    status, body = _post(gate, None, raw=json.dumps({"repository": REPO, "pr_number": 7,
                                                     "head_sha": SHA}))
    assert status == 409 and "head moved" in body["error"]
    # A push between the head read and the file read.
    gate.github.put(REPO, 7, SHA, [_github_file(_change())])
    gate.github.head_after_files = "c" * 40
    status, body = _post(gate, None, raw=json.dumps({"repository": REPO, "pr_number": 7,
                                                     "head_sha": SHA}))
    assert status == 409 and "head moved" in body["error"]
    assert _gate_rows(gate) == {}


def test_a_github_failure_is_503_records_nothing_and_logs_no_token(gate, capsys):
    github = sys.modules["activity.github"]
    gate.github.put(REPO, 7, SHA, [_github_file(_change())])
    for failure in (github.GitHubError(0, "/repos/x/pulls/7"),
                    github.RateLimited("/repos/x/pulls/7", None, 60),
                    github.NotAccessible(404, "/repos/x/pulls/7"),
                    github.NoToken("the secret holds no token")):
        gate.github.fail = failure
        status, body = _post(gate, None, raw=json.dumps({"repository": REPO, "pr_number": 7,
                                                         "head_sha": SHA}))
        assert status == 503 and "could not read the pull request" in body["error"]
    assert _written(gate) == {}
    gate.gate_api._token = "ghp_" + "T" * 36
    gate.github.fail = github.GitHubError(401, "/repos/x/pulls/7")
    assert _post(gate, None, raw=json.dumps({"repository": REPO, "pr_number": 7,
                                             "head_sha": SHA}))[0] == 503
    assert gate.gate_api._token is None
    assert "ghp_" not in capsys.readouterr().out


def test_the_github_client_reads_a_pull_request_and_its_file_pages():
    sys.path.insert(0, str(LAMBDA_DIR))
    from activity import github

    seen = []

    class Response:
        def __init__(self, body, link=None):
            self.status, self._body = 200, json.dumps(body).encode()
            self.headers = {"X-RateLimit-Remaining": "4000", **({"Link": link} if link else {})}

        def read(self):
            return self._body

        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        seen.append((request.full_url, timeout))
        url = request.full_url
        if url.endswith("/pulls/7"):
            return Response({"state": "open", "head": {"sha": SHA}})
        found = re.search(r"&page=(\d+)", url)
        page = int(found.group(1)) if found else 1
        nxt = f'<{github.API}/repos/o/r/pulls/7/files?per_page=100&page={page + 1}>; rel="next"'
        return Response([{"filename": f"f{page}.py"}], nxt)

    client = github.GitHub("token", opener, timeout=4)
    assert client.pull("o/r", 7)["head"]["sha"] == SHA
    files, complete = client.pull_files("o/r", 7, 3)
    assert [f["filename"] for f in files] == ["f1.py", "f2.py", "f3.py"] and complete is False
    assert all(timeout == 4 for _, timeout in seen)
    assert seen[1][0] == f"{github.API}/repos/o/r/pulls/7/files?per_page=100"
    with pytest.raises(github.GitHubError):
        client.pull("o/r", 0)
    with pytest.raises(github.GitHubError):
        client.pull_files("o/r", True, 3)
    assert "token" not in repr(client)


# --- scan to gate status ---------------------------------------------------------


def test_a_clean_pull_request_passes_without_asking_decider(gate):
    status, view = _post(gate, _body([_change(), _change("README.md", "added", "# Volcan")]))
    assert status == 202
    assert set(view) == {"gate_id", "status", "summary"}
    assert view["status"] == "PASSED"
    assert view["summary"] == {"label": "deterministic scan", "verdict": "clean", "files": 2,
                               "flagged_files": 0, "files_by_rule": {}, "decider": "not_needed"}
    row = _row(gate, view["gate_id"])
    assert row["status"] == "DONE" and row["lane"] == "gate" and row["project"] == "Volcan"
    assert row["repository"] == REPO and row["pr_number"] == 7 and row["head_sha"] == SHA
    # One transaction records the run: the row, both events and the daily
    # counter, so it is never seen STARTING. The one before it, ahead of
    # any GitHub read, is the repository's hourly POST counter alone.
    post, record = gate.table.dynamo.transactions
    assert len(post) == 1
    assert post[0]["Update"]["Key"]["approval_id"].startswith(_post_counter_prefix(gate))
    keys = [spec.get("Key", spec.get("Item"))["approval_id"]
            for entry in record for spec in entry.values()]
    assert len(keys) == 4 and keys[0].startswith("gate-repo-")
    assert keys[1:] == [view["gate_id"]] * 3
    assert _event_types(gate, view["gate_id"]) == ["RunRequested", "RunCompleted"]
    assert gate.sagemaker.described == [] and gate.sagemaker.invoked == []


def test_a_gate_row_outlives_its_pause_so_a_rerun_days_later_finds_it(gate):
    domain = sys.modules["approval.domain"]
    status, view = _post(gate, _body([_change("infra/main.tf")]))
    row = _row(gate, view["gate_id"])
    assert row["ttl"] >= row["created_ts"] + domain.GATE_ROW_TTL_SECONDS
    assert domain.GATE_ROW_TTL_SECONDS >= 30 * 86400
    assert row["expires_ts"] == row["created_ts"] + gate.common.TTL_SECONDS


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (_change(".github/workflows/ci.yml"), "workflow_changed"),
        (_change("infra/stacks/app.py"), "infrastructure_changed"),
        (_change("db/migrations/0002.sql"), "database_migration"),
        (_change("src/app.py", "removed", "", additions=0, deletions=40), "file_deleted"),
        (_change("schema.sql", excerpt="DROP TABLE users;"), "database_migration"),
        (_change("conf.py", excerpt=f"key = '{AKIA}'"), "secret_in_added_line"),
        (_change(".github/workflows/ci.yml", "renamed", previous_path="old.yml"),
         "workflow_changed"),
        (_change("docs/x.yml", "renamed", previous_path=".github/workflows/deploy.yml"),
         "workflow_changed"),
    ],
)
def test_a_flagged_change_holds_the_gate_for_a_person(gate, change, rule):
    status, view = _post(gate, _body([_change(), change]))
    assert status == 202 and view["status"] == "PENDING"
    summary = view["summary"]
    assert summary["verdict"] == "flagged" and summary["flagged_files"] == 1
    assert summary["files_by_rule"][rule] == 1 and summary["decider"] == "asleep"
    row = _row(gate, view["gate_id"])
    assert row["status"] == "PENDING" and row["lane"] == "gate"
    card = json.loads(row["card"])
    assert card["layer"] == "mergegate" and card["tool"] == "merge_gate"
    assert card["risk"] == "" and card["confidence"] is None
    assert change["path"] in card["reason"] and rule in card["reason"]
    assert "src/app.py (" not in card["reason"] or change["path"] == "src/app.py"
    assert card["changed_files"] == [change["path"]]
    assert _event_types(gate, view["gate_id"]) == ["RunRequested", "RunPaused"]
    paused = gate.table.dynamo.events_of(view["gate_id"])[1]["data"]
    # The pause is the scan's: no Decider score and no path on the bus.
    assert paused["confidence"] is None and paused["risk"] == ""
    assert paused["layer"] == "mergegate" and paused["tool"] == "merge_gate"
    assert change["path"] not in json.dumps(paused)


@pytest.mark.parametrize(
    "excerpt",
    [
        # An added line whose text starts with ++ (no file headers in
        # GitHub's patch), and fragments after separators splitlines()
        # would have cut off: every one is scanned.
        f'++i; const k = "{AKIA}";',
        f'x = 1;\n++ k = "{AKIA}"',
        f'x = 1;\x0c++k = "{AKIA}"',
        f'x = 1; ++k = "{AKIA}"',
        f'x = 1;\x1e++k = "{AKIA}"',
        f'x = 1;\x85k = "{AKIA}"',
    ],
)
def test_no_added_line_escapes_the_secret_scan(gate, excerpt):
    status, view = _post(gate, _body([_change("app.js", excerpt=excerpt)]))
    assert status == 202 and view["status"] == "PENDING"
    assert view["summary"]["files_by_rule"] == {"secret_in_added_line": 1}


def test_a_drop_after_a_form_feed_is_still_found(gate):
    _, view = _post(gate, _body([_change("q.sql", excerpt="x = 1;\x0c++DROP TABLE users;")]))
    assert view["summary"]["files_by_rule"] == {"database_migration": 1}


@pytest.mark.parametrize(
    "body",
    [
        _body([_change(excerpt="x" * 25000)]),
        _body([_change(excerpt="x\n" * 12000)]),
        _body([_change(excerpt="added lines", no_patch=True)]),
        _body(files_truncated=True),
        # GitHub lists no file at all: nothing was read, so not clean.
        _body([]),
    ],
)
def test_what_was_not_read_is_never_called_clean(gate, body):
    status, view = _post(gate, body)
    assert status == 202 and view["status"] == "PENDING"
    assert view["summary"]["files_by_rule"]["scan_incomplete"] == 1


def test_a_file_under_the_scan_cap_is_read_whole_and_passes(gate):
    # 8000 characters was the old excerpt cap; the scan reads up to 20000.
    status, view = _post(gate, _body([_change("tests/test_big.py", excerpt="x = 1\n" * 3000)]))
    assert status == 202 and view["status"] == "PASSED"


def test_a_changed_files_count_above_what_was_read_marks_the_list_cut(gate):
    gate.github.put(REPO, 7, SHA, [_github_file(_change())], changed_files=301)
    status, view = _post(gate, None, raw=json.dumps({"repository": REPO, "pr_number": 7,
                                                     "head_sha": SHA}))
    assert status == 202 and view["status"] == "PENDING"
    card = _card(gate, view)
    assert "(files not read)" in card["reason"] and view["summary"]["decider"] == "not_asked"
    assert gate.sagemaker.described == []


def test_a_binary_file_is_checked_by_path_only(gate):
    status, view = _post(gate, _body([_change("logo.png", "added", "", additions=0)]))
    assert status == 202 and view["status"] == "PASSED"


def _module_doc(path):
    return " ".join(ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))).split())


def test_the_guarantee_names_the_binary_exception_wherever_it_is_stated():
    # R3-B1 (J2): a binary file's content is never read, so no docstring
    # may say that nothing unread is ever called clean without that.
    policy_doc = _module_doc(LAMBDA_DIR / "gate" / "policy.py")
    assert ("A binary file (no additions, no patch) is checked by its path only; its content "
            "is never read. Apart from binary files, what was not read is never called "
            "clean.") in policy_doc
    assert "by its path only. What was not read is never called clean." not in policy_doc
    api_doc = _module_doc(LAMBDA_DIR / "gate_api.py")
    assert "the gate never passes what it could not read." not in api_doc
    assert ("the gate never passes a pull request it could not read from GitHub. A binary "
            "file (no additions, no patch) is checked by its path only; its content is never "
            "read") in api_doc
    scan_doc = _module_doc(LAMBDA_DIR / "activity" / "scan.py")
    assert "It flags the commit: what was not read is not called clean." not in scan_doc
    assert ("is checked by its path only and its content is never read, and the added lines "
            "of one file past SCAN_CHARS are not read (see below); apart from those two, what "
            "was not read is not called clean.") in scan_doc
    for path in (WORKFLOW, ROOT / "approvals" / "console" / "app.js",
                 ROOT / "approvals" / "console" / "index.html"):
        assert "called clean" not in path.read_text(encoding="utf-8"), path


@pytest.mark.parametrize("path", [
    "a.txt\nThe deterministic scan flagged 0 of 1 changed files",
    "a.txt\rStrands Decider 2B, advisory: benign at 0.99",
    "a.txt Merge gate: forged",
    "a.txt\x85forged",
])
def test_a_path_cannot_forge_a_line_of_the_card(gate, path):
    status, view = _post(gate, _body([_change(path)]))
    assert status == 202 and view["status"] == "PENDING"
    card = _card(gate, view)
    assert path not in card["reason"] and path not in json.dumps(card["changed_files"])
    assert "the path holds control characters" in card["reason"]
    assert len(card["reason"].split("\n")) == 7
    assert view["summary"]["files_by_rule"] == {"scan_incomplete": 1}


def test_a_long_path_is_cut_and_flagged_never_refused(gate):
    long_path = "src/" + "d" * 500 + ".py"
    status, view = _post(gate, _body([_change(long_path)]))
    assert status == 202 and view["status"] == "PENDING"
    card = _card(gate, view)
    assert "the path was longer than 400 characters and was cut" in card["reason"]
    assert long_path not in card["reason"]


def test_a_secret_is_never_quoted_on_the_card_the_summary_or_to_decider(gate):
    secret = "ghp_" + "Z" * 36
    gate.sagemaker.copies = 1
    status, view = _post(gate, _body([_change("app/settings.py", excerpt=f"TOKEN = '{secret}'"),
                                      _change(".github/workflows/x.yml", excerpt="run: make")]))
    assert status == 202 and view["status"] == "PENDING"
    stored = repr(gate.table.dynamo.tables)
    assert secret not in stored and secret not in json.dumps(view)
    sent = gate.sagemaker.invoked[0]["Body"].decode()
    assert secret not in sent and "app/settings.py" in sent and "run: make" in sent


def test_the_card_lists_at_most_twenty_flagged_files_and_counts_the_rest(gate):
    changes = [_change(f"infra/f{i}.tf") for i in range(25)]
    status, view = _post(gate, _body(changes))
    card = _card(gate, view)
    assert "... and 5 more flagged files." in card["reason"] and len(card["changed_files"]) == 20
    assert view["summary"]["flagged_files"] == 25


# --- Decider: never invoked without a copy, never the gate ------------------------------


@pytest.mark.parametrize(
    "sagemaker",
    [
        FakeSageMaker(copies=0),
        FakeSageMaker(copies=1, status="Updating"),
        FakeSageMaker(copies=None),
        # Scale-in under way: the copy is still there, but going.
        FakeSageMaker(copies=1, desired=0),
    ],
)
def test_a_cold_gpu_is_never_invoked_and_a_person_decides(gate, monkeypatch, sagemaker):
    monkeypatch.setattr(gate.gate_api.decider, "_clients", lambda: (sagemaker, sagemaker))
    status, view = _post(gate, _body([_change("infra/main.tf")]))
    assert status == 202 and view["status"] == "PENDING"
    assert len(sagemaker.described) == 1 and sagemaker.invoked == []
    assert sagemaker.described[0] == {"InferenceComponentName": "lachesis-decider-2b"}
    card = _card(gate, view)
    assert "Decider asleep: a person decides" in card["reason"]
    assert view["summary"]["decider"] == "asleep"


def test_a_describe_error_is_a_failure_never_called_asleep(gate, monkeypatch):
    sagemaker = FakeSageMaker(describe_error=RuntimeError("AccessDenied"))
    monkeypatch.setattr(gate.gate_api.decider, "_clients", lambda: (sagemaker, sagemaker))
    status, view = _post(gate, _body([_change("infra/main.tf")]))
    assert status == 202 and view["status"] == "PENDING" and sagemaker.invoked == []
    assert view["summary"]["decider"] == "failed"
    assert "Decider did not answer" in _card(gate, view)["reason"]


def test_clients_that_cannot_be_built_fail_closed(gate, monkeypatch):
    def broken():
        raise RuntimeError("no region")

    monkeypatch.setattr(gate.gate_api.decider, "_clients", broken)
    status, view = _post(gate, _body([_change("infra/main.tf")]))
    assert status == 202 and view["status"] == "PENDING" and view["summary"]["decider"] == "failed"


@pytest.mark.parametrize("answer", [
    {"choice": "benign", "confidence": 0.99},
    {"choice": "destructive_write", "confidence": 0.93},
])
def test_a_warm_decider_is_asked_once_and_never_clears_a_flag(gate, answer):
    gate.sagemaker.copies, gate.sagemaker.answer = 1, answer
    status, view = _post(gate, _body([_change("infra/main.tf"), _change("db/migrate/1.sql")]))
    assert status == 202 and view["status"] == "PENDING"
    assert len(gate.sagemaker.invoked) == 1
    call = gate.sagemaker.invoked[0]
    assert call["EndpointName"] == "lachesis-decider"
    assert call["InferenceComponentName"] == "lachesis-decider-2b"
    request = json.loads(call["Body"])
    assert set(request) == {"state", "questions"} and set(request["questions"]) == {"risk_class"}
    card = _card(gate, view)
    line = (f"Strands Decider 2B, advisory: {answer['choice']} "
            f"at {answer['confidence']:.2f}, 120 ms.")
    assert line in card["reason"] and "It does not clear a flag" in card["reason"]
    assert card["confidence"] is None and card["layer"] == "mergegate"
    assert view["summary"]["decider"] == "answered"


def test_decider_readings_are_capped_per_hour(gate, monkeypatch):
    service = gate.gate_api.service
    monkeypatch.setattr(service, "GATE_DECIDER_HOURLY_CAP", 2)
    gate.sagemaker.copies = 1
    states = [_post(gate, _body([_change("infra/main.tf")], sha=ch * 40))[1]["summary"]["decider"]
              for ch in "abc"]
    assert states == ["answered", "answered", "skipped"] and len(gate.sagemaker.invoked) == 2
    held = [row for key, row in gate.table.items.items() if str(key).startswith("appr-gate-")]
    assert any("hourly reading cap" in json.loads(row["card"])["reason"] for row in held)


def test_decider_is_not_asked_without_time_left(gate):
    class Context:
        def get_remaining_time_in_millis(self):
            return 5000

    gate.sagemaker.copies = 1
    _, view = _post(gate, _body([_change("infra/main.tf")]), lambda_context=Context())
    assert view["summary"]["decider"] == "skipped" and gate.sagemaker.invoked == []
    assert gate.sagemaker.described == []


@pytest.mark.parametrize("answer", [
    {"choice": "harmless", "confidence": 0.9},
    {"choice": "benign", "confidence": 1.5},
    {"choice": "benign", "confidence": float("nan")},
    {"choice": "benign"},
])
def test_an_unusable_decider_answer_is_a_failure_not_a_score(gate, answer):
    gate.sagemaker.copies, gate.sagemaker.answer = 1, answer
    status, view = _post(gate, _body([_change("infra/main.tf")]))
    card = _card(gate, view)
    assert view["summary"]["decider"] == "failed" and "Decider did not answer" in card["reason"]


def test_the_risk_question_is_the_patterns_question():
    sys.path.insert(0, str(LAMBDA_DIR))
    from gate import decider

    from lachesis import questions

    assert decider.risk_question() == questions.risk_question()
    assert decider.RISK_CLASSES == questions.RISK_CLASSES


# --- one run per pull-request head, and the caps -------------------------------------


def test_a_rerun_finds_the_same_run_and_a_closed_one_opens_the_next_round(gate):
    gate.sagemaker.copies = 1
    first = _post(gate, _body([_change("infra/main.tf")]))
    calls = len(gate.github.calls)
    again = _post(gate, _body([_change("infra/main.tf")]))
    assert first[0] == 202 and again[0] == 200 and again[1]["gate_id"] == first[1]["gate_id"]
    # A re-run reads the head only, never the files again.
    assert gate.github.calls[calls:] == [("pull", REPO, 7)]
    assert len(gate.sagemaker.invoked) == 1
    gate_id = first[1]["gate_id"]
    assert re.fullmatch(r"appr-gate-[0-9a-f]{40}-0", gate_id)
    gate.table.items[gate_id]["status"] = "EXPIRED"
    third = _post(gate, _body([_change("infra/main.tf")]))
    assert third[0] == 202 and third[1]["gate_id"] == gate_id[:-1] + "1"
    other_head = _post(gate, _body([_change("infra/main.tf")], sha="b" * 40))
    assert other_head[1]["gate_id"] not in {gate_id, third[1]["gate_id"]}


def test_every_round_used_is_409(gate):
    policy = gate.gate_api.policy
    pull = policy.parse(_body())
    for round_ in range(policy.MAX_ROUNDS):
        gate.table.items[policy.gate_id(pull, round_, "main")] = {
            "approval_id": policy.gate_id(pull, round_, "main"), "status": "ERROR",
            "lane": "gate"}
    assert _post(gate, _body())[0] == 409


def test_new_gate_runs_are_capped_per_repository_per_day(gate, monkeypatch):
    service = gate.gate_api.service
    monkeypatch.setattr(service, "GATE_REPO_DAILY_CAP", 2)
    codes = [_post(gate, _body(sha=ch * 40))[0] for ch in "abc"]
    assert codes == [202, 202, 429]
    assert len([k for k in gate.table.items if str(k).startswith("appr-gate-")]) == 2
    # Another repository has its own counter.
    other = _body(repository=OTHER_REPO, sha="d" * 40)
    assert _post(gate, other, _claims(repository=OTHER_REPO))[0] == 202


def test_a_concurrent_writer_of_the_same_head_is_answered_with_its_run(gate, monkeypatch):
    service = gate.gate_api.service
    real = service.record_gate

    def raced(gate_id, *args, **kwargs):
        real(gate_id, *args, **kwargs)
        raise gate.gate_api.domain.Conflict()

    monkeypatch.setattr(service, "record_gate", raced)
    status, view = _post(gate, _body())
    assert status == 200 and view["status"] == "PASSED"


def test_a_failed_write_is_503_and_names_nothing(gate, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise ValueError("boom with a path infra/main.tf")

    monkeypatch.setattr(gate.gate_api.service, "record_gate", broken)
    assert _post(gate, _body())[0] == 503
    out = capsys.readouterr().out
    assert "ValueError" in out and "infra/main.tf" not in out and REPO not in out


def test_contention_is_a_retryable_503(gate, monkeypatch):
    def busy(*args, **kwargs):
        raise gate.gate_api.domain.Busy()

    monkeypatch.setattr(gate.gate_api.service, "record_gate", busy)
    assert _post(gate, _body())[0] == 503


# --- B7b verify: base branch, POST cap, time budget, counters, card size -----------------


def _bare(sha=SHA):
    return {"repository": REPO, "pr_number": 7, "head_sha": sha}


def test_a_retarget_reads_the_new_bases_files_and_never_keeps_the_old_verdict(gate):
    # J1: the same head, first against main with a README only.
    gate.github.put(REPO, 7, SHA, [_github_file(_change("README.md", "added", "# Volcan"))])
    status, first = _post(gate, _bare())
    assert status == 202 and first["status"] == "PASSED" and first["summary"]["files"] == 1
    # Retargeted: GitHub now lists a workflow change for the same head.
    gate.github.put(REPO, 7, SHA, [_github_file(_change("README.md", "added", "# Volcan")),
                                   _github_file(_change(".github/workflows/deploy.yml"))],
                    base="release")
    calls = len(gate.github.calls)
    status, second = _post(gate, _bare())
    assert status == 202 and second["status"] == "PENDING"
    assert second["gate_id"] != first["gate_id"] and second["summary"]["files"] == 2
    assert second["summary"]["files_by_rule"] == {"workflow_changed": 1}
    assert ("files", REPO, 7, 3) in gate.github.calls[calls:]
    # A re-run against the new base finds its run; the old base's run
    # keeps its own verdict and is never answered for the new base.
    status, again = _post(gate, _bare())
    assert status == 200 and again["gate_id"] == second["gate_id"]
    assert again["status"] == "PENDING"
    policy = gate.gate_api.policy
    request = policy.parse(_bare())
    assert policy.gate_id(request, 0, "main") == first["gate_id"]
    assert policy.gate_id(request, 0, "release") == second["gate_id"]
    # GitHub always names the base; without one the id is still stable.
    assert gate.gate_api._base({}) == "" and gate.gate_api._base({"base": {"ref": 3}}) == ""


def test_a_base_change_between_the_reads_is_409_and_records_nothing(gate):
    gate.github.put(REPO, 7, SHA, [_github_file(_change())])
    gate.github.base_after_files = "release"
    status, body = _post(gate, _bare())
    assert status == 409 and "base branch changed" in body["error"]
    assert _gate_rows(gate) == {}


def test_gate_posts_are_capped_per_repository_per_hour_before_github_is_read(gate, monkeypatch):
    # J7: every POST costs at least one read of the shared token.
    service = gate.gate_api.service
    monkeypatch.setattr(service, "GATE_POST_HOURLY_CAP", 2)
    assert _post(gate, _body())[0] == 202
    assert _post(gate, _body())[0] == 200
    calls = len(gate.github.calls)
    # A made-up head is refused before GitHub is read, like a re-run.
    status, body = _post(gate, None, raw=json.dumps(_bare("f" * 40)))
    assert status == 429 and "hourly gate requests" in body["error"]
    assert gate.github.calls[calls:] == []
    (key,) = [k for k in gate.table.items if str(k).startswith("gate-post-")]
    assert REPO not in key and key.startswith(_post_counter_prefix(gate))
    assert gate.table.items[key]["count"] == 2
    # Another repository has its own counter.
    other = _body(repository=OTHER_REPO, sha="d" * 40)
    assert _post(gate, other, _claims(repository=OTHER_REPO))[0] == 202


def test_a_post_counter_that_cannot_be_written_is_503_and_reads_nothing(gate, monkeypatch):
    def broken(repository):
        raise RuntimeError("table unavailable")

    monkeypatch.setattr(gate.gate_api.service, "take_gate_post", broken)
    assert _post(gate, _body())[0] == 503
    assert gate.github.calls == []


def test_the_time_budget_is_checked_again_before_the_invoke(gate):
    # J3: enough time before the describe, too little after it.
    class Context:
        def __init__(self):
            self.answers = [13000, 4000]

        def get_remaining_time_in_millis(self):
            return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    gate.sagemaker.copies = 1
    _, view = _post(gate, _body([_change("infra/main.tf")]), lambda_context=Context())
    assert view["summary"]["decider"] == "skipped"
    assert len(gate.sagemaker.described) == 1 and gate.sagemaker.invoked == []
    # The hourly reading was not spent.
    assert not [k for k in gate.table.items if str(k).startswith("gate-decider-")]
    assert "hourly reading cap or time budget" in _card(gate, view)["reason"]


def test_an_empty_file_list_is_never_called_a_cut_list(gate):
    # J4: GitHub listed no file; the card does not say the list was cut.
    status, view = _post(gate, _body([]))
    assert status == 202 and view["status"] == "PENDING"
    reason = _card(gate, view)["reason"]
    assert "GitHub listed no changed files" in reason
    assert "only the file list was cut" not in reason
    assert ("Decider not asked: no flagged file to describe (the file list was cut or empty). "
            "A person decides.") in reason


def test_a_contended_counter_is_tried_again_and_never_read_as_the_cap(gate, monkeypatch):
    # J5: a cancellation caused only by a concurrent transaction.
    uow = sys.modules["approval.uow"]
    pauses = []
    monkeypatch.setattr(uow, "_sleep", pauses.append)
    gate.table.dynamo.inject = [["TransactionConflict"]] * uow.CONTENTION_RETRIES
    assert gate.gate_api.service.take_gate_reading() is True
    assert len(pauses) == uow.CONTENTION_RETRIES and not gate.table.dynamo.inject
    (key,) = [k for k in gate.table.items if str(k).startswith("gate-decider-")]
    assert gate.table.items[key]["count"] == 1
    # Contention that outlasts the retries is raised, never read as the cap.
    gate.table.dynamo.inject = [["TransactionConflict"]] * (uow.CONTENTION_RETRIES + 1)
    with pytest.raises(ClientError):
        gate.gate_api.service.take_gate_reading()
    assert gate.table.items[key]["count"] == 1
    # The cap itself is still False, after no retry.
    pauses.clear()
    gate.table.items[key]["count"] = gate.gate_api.service.GATE_DECIDER_HOURLY_CAP
    assert gate.gate_api.service.take_gate_reading() is False and pauses == []


def test_a_card_of_long_non_ascii_paths_fits_its_dynamodb_row(gate):
    # J8: astral characters cost 12 bytes each in the stored card.
    emoji = "\U0001F600"
    changes = [_change(emoji * 395 + f"{i}", "renamed",
                       previous_path=".github/workflows/infra/migrations/" + emoji * 360 + f"{i}")
               for i in range(25)]
    status, view = _post(gate, _body(changes))
    assert status == 202 and view["status"] == "PENDING"
    assert view["summary"]["flagged_files"] == 25
    stored = _row(gate, view["gate_id"])["card"]
    policy = gate.gate_api.policy
    assert len(stored) <= policy.CARD_MAX_BYTES < 400 * 1024
    card = json.loads(stored)
    listed = len(card["changed_files"])
    assert 0 < listed < policy.CARD_CHANGES
    assert f"... and {25 - listed} more flagged files." in card["reason"]
    assert card["reason"].count(" (renamed, +1 -0)") == listed
    assert card["reason"].endswith(gate.gate_api.decider.Reading("asleep").line())


@pytest.mark.parametrize("mark", ["\u202e", "\u2066", "\u200b", "\ufeff", "\u061c",
                                  "\U000e0041"])
def test_a_format_character_in_a_path_is_escaped_and_held(gate, mark):
    # J9: a bidi override, an isolate, a zero-width mark or a tag.
    path = f"secrets/{mark}env.dorp.txt"
    status, view = _post(gate, _body([_change(path)]))
    assert status == 202 and view["status"] == "PENDING"
    card = _card(gate, view)
    assert mark not in card["reason"] and mark not in json.dumps(card["changed_files"],
                                                                 ensure_ascii=False)
    assert mark.encode("unicode_escape").decode("ascii") in card["reason"]
    assert "the path holds control characters" in card["reason"]
    assert view["summary"]["files_by_rule"] == {"scan_incomplete": 1}


# --- GET /gate/{id} ------------------------------------------------------------------


def test_get_answers_status_and_summary_only_and_only_to_its_repository(gate):
    _, view = _post(gate, _body([_change("infra/secret_plan.tf")]))
    status, read = _get(gate, view["gate_id"])
    assert status == 200 and read == view
    assert "infra/secret_plan.tf" not in json.dumps(read)
    assert _get(gate, view["gate_id"], _claims(repository=OTHER_REPO))[0] == 404
    assert _get(gate, "appr-" + "x" * 30)[0] == 404
    assert _get(gate, "appr-gate-" + "0" * 40 + "-0")[0] == 404


def test_get_never_reads_another_lane(gate):
    gate.common._table.items["appr-gate-" + "1" * 40 + "-0"] = {
        "approval_id": "appr-gate-" + "1" * 40 + "-0", "status": "PENDING", "lane": "approval",
        "repository": REPO}
    assert _get(gate, "appr-gate-" + "1" * 40 + "-0")[0] == 404


def test_an_unknown_gate_route_is_404(gate):
    event = {"routeKey": "GET /gate", "requestContext": _context(None)}
    assert gate.gate_api.handler(event, None)["statusCode"] == 404


# --- lane isolation -------------------------------------------------------------------


def test_the_try_lane_never_reads_or_decides_a_gate_run(gate):
    _, view = _post(gate, _body([_change("infra/main.tf")]))
    gate_id = view["gate_id"]
    for route in ("GET /try/{token}", "GET /try/{token}/events"):
        out = gate.api.handler(_event(route, params={"token": gate_id}), None)
        assert out["statusCode"] == 404, route
    out = gate.api.handler(
        _event("POST /try/{token}/decision", body={"decision": "approve"},
               params={"token": gate_id}),
        None,
    )
    assert out["statusCode"] == 404 and _row(gate, gate_id)["status"] == "PENDING"


def test_the_approver_queue_lists_gate_runs_by_project_and_others_get_403(gate):
    _, view = _post(gate, _body([_change("infra/main.tf")]))
    out = gate.api.handler(_event("GET /approvals", APPROVER), None)
    rows = json.loads(out["body"])["approvals"]
    assert [(row["approval_id"], row["lane"], row["project"]) for row in rows] == [
        (view["gate_id"], "gate", "Volcan")]
    assert "infra/main.tf" in rows[0]["card"]["reason"]
    detail = gate.api.handler(
        _event("GET /approvals/{id}", APPROVER, params={"id": view["gate_id"]}), None)
    assert detail["statusCode"] == 200
    ledger = gate.api.handler(
        _event("GET /approvals/{id}/events", APPROVER, params={"id": view["gate_id"]}), None)
    types = [e["type"] for e in json.loads(ledger["body"])["events"]]
    assert types == ["RunRequested", "RunPaused"]
    for route in ("GET /approvals", "GET /approvals/{id}"):
        assert gate.api.handler(_event(route, PLAIN_USER, params={"id": view["gate_id"]}),
                                None)["statusCode"] == 403


def test_try_pauses_cannot_push_gate_runs_out_of_the_approver_queue(gate):
    _, view = _post(gate, _body([_change("infra/main.tf")]))
    created = _row(gate, view["gate_id"])["created_ts"]
    for i in range(160):
        key = f"appr-try{i:04d}"
        gate.table.items[key] = {"approval_id": key, "status": "PENDING", "lane": "try",
                                 "created_ts": created + 10 + i, "card": "{}"}
    out = gate.api.handler(_event("GET /approvals", APPROVER), None)
    rows = json.loads(out["body"])["approvals"]
    assert [row["approval_id"] for row in rows] == [view["gate_id"]]


def test_gate_rows_cannot_push_approval_pauses_off_the_approver_queue(gate):
    # J12: one limit per lane.
    now = int(time.time())
    gate.table.items["appr-confirm"] = {"approval_id": "appr-confirm", "status": "PENDING",
                                        "lane": "approval", "created_ts": now - 600, "card": "{}"}
    for i in range(60):
        key = f"appr-gate-{i:040d}-0"
        gate.table.items[key] = {"approval_id": key, "status": "PENDING", "lane": "gate",
                                 "created_ts": now + i, "card": "{}", "head_sha": SHA}
    out = gate.api.handler(_event("GET /approvals", APPROVER), None)
    rows = json.loads(out["body"])["approvals"]
    assert "appr-confirm" in [row["approval_id"] for row in rows]
    assert [row["lane"] for row in rows].count("gate") == 50


def test_the_queue_tells_one_heads_gate_card_from_another(gate):
    # J11: the head a gate run is for is in its queue row.
    _post(gate, _body([_change("infra/main.tf")]))
    _post(gate, _body([_change("infra/main.tf")], sha="b" * 40))
    out = gate.api.handler(_event("GET /approvals", APPROVER), None)
    rows = json.loads(out["body"])["approvals"]
    assert sorted(row["head"] for row in rows) == ["a" * 12, "b" * 12]
    # Only a gate run carries one.
    gate.table.items["appr-confirm"] = {"approval_id": "appr-confirm", "status": "PENDING",
                                        "lane": "approval", "created_ts": 1, "card": "{}"}
    out = gate.api.handler(_event("GET /approvals", APPROVER), None)
    plain = [row for row in json.loads(out["body"])["approvals"] if row["lane"] == "approval"]
    assert "head" not in plain[0]


def _queue(world):
    out = world.api.handler(_event("GET /approvals", APPROVER), None)
    assert out["statusCode"] == 200
    return out, json.loads(out["body"])


def _detail(world, gate_id):
    out = world.api.handler(_event("GET /approvals/{id}", APPROVER, params={"id": gate_id}),
                            None)
    assert out["statusCode"] == 200
    return json.loads(out["body"])


def test_two_runs_of_one_head_against_two_bases_are_told_apart(gate):
    # R3-B3: a retarget opens a second run of the same head; the row, the
    # card and the queue name the base, so the live card is told apart.
    flagged = [_github_file(_change("infra/main.tf"))]
    gate.github.put(REPO, 7, SHA, flagged)
    status, first = _post(gate, _bare())
    assert status == 202 and first["status"] == "PENDING"
    gate.github.put(REPO, 7, SHA, flagged, base="release")
    status, second = _post(gate, _bare())
    assert status == 202 and second["status"] == "PENDING"
    assert second["gate_id"] != first["gate_id"]
    assert _row(gate, first["gate_id"])["base_ref"] == "main"
    assert _row(gate, second["gate_id"])["base_ref"] == "release"
    _, body = _queue(gate)
    rows = {row["approval_id"]: row for row in body["approvals"]}
    assert rows[first["gate_id"]]["head"] == rows[second["gate_id"]]["head"] == SHA[:12]
    assert rows[first["gate_id"]]["base"] == "main"
    assert rows[second["gate_id"]]["base"] == "release"
    first_card, second_card = _card(gate, first), _card(gate, second)
    assert first_card["reason"] != second_card["reason"]
    assert first_card["reason"].split("\n")[0] == (
        f"Merge gate: pull request #7 in {REPO} (Volcan), head {SHA[:12]} into main.")
    assert second_card["reason"].split("\n")[0].endswith(f"head {SHA[:12]} into release.")
    assert rows[second["gate_id"]]["card"]["reason"].startswith(
        second_card["reason"].split("\n")[0])
    detail = _detail(gate, second["gate_id"])
    assert detail["base"] == "release" and detail["card"]["reason"] == second_card["reason"]
    # GET /gate/{id} still answers counts only: no branch name.
    assert "release" not in json.dumps(_get(gate, second["gate_id"])[1])


def test_a_base_branch_name_is_escaped_on_the_card_and_in_the_queue(gate):
    # A branch name may hold format characters, like a path (J9).
    base = "main‮txt.gnp"
    shown = "main\\u202etxt.gnp"
    gate.github.put(REPO, 7, SHA, [_github_file(_change("infra/main.tf"))], base=base)
    status, view = _post(gate, _bare())
    assert status == 202 and view["status"] == "PENDING"
    # The run id holds the branch itself.
    policy = gate.gate_api.policy
    assert view["gate_id"] == policy.gate_id(policy.parse(_bare()), 0, base)
    header = _card(gate, view)["reason"].split("\n")[0]
    assert "‮" not in header and header.endswith(f" into {shown}.")
    assert _row(gate, view["gate_id"])["base_ref"] == shown
    _, body = _queue(gate)
    assert [row["base"] for row in body["approvals"]] == [shown]
    # A long name is cut on the card and cut shorter in the queue row.
    long_base = "b" * 300
    gate.github.put(REPO, 7, SHA, [_github_file(_change("infra/main.tf"))], base=long_base)
    _, long_view = _post(gate, _bare())
    header = _card(gate, long_view)["reason"].split("\n")[0]
    assert header.endswith(" into " + "b" * policy.MAX_REF_CHARS + ".")
    assert _detail(gate, long_view["gate_id"])["base"] == "b" * 64


def _large_card_files():
    # The large-card probe of J8: 25 renames of long astral paths.
    emoji = "\U0001F600"
    return [_github_file(_change(emoji * 395 + f"{i}", "renamed",
                                 previous_path=".github/workflows/infra/migrations/"
                                 + emoji * 360 + f"{i}"))
            for i in range(25)]


def test_large_gate_cards_never_fail_the_approver_queue(gate):
    # R3-B4: whole cards of about 300 KB each would pass Lambda's 6 MB
    # response limit, and the whole queue would fail with them.
    now = int(time.time())
    confirm = {"reason": "write_file needs a person to approve it.", "tool": "write_file"}
    gate.table.items["appr-confirm"] = {"approval_id": "appr-confirm", "status": "PENDING",
                                        "lane": "approval", "created_ts": now - 60,
                                        "card": json.dumps(confirm)}
    files, ids = _large_card_files(), []
    for i in range(22):
        sha = format(i + 1, "040x")
        gate.github.put(REPO, 7, sha, files)
        status, view = _post(gate, _bare(sha))
        assert status == 202 and view["status"] == "PENDING"
        ids.append(view["gate_id"])
    whole = sum(len(json.dumps(gate.common.public_view(_row(gate, i)))) for i in ids)
    assert whole > 6 * 1024 * 1024
    out, body = _queue(gate)
    assert len(out["body"]) < 1_000_000 and len(json.dumps(out)) < 2_000_000
    assert "gate_rows_omitted" not in body
    rows = {row["approval_id"]: row for row in body["approvals"]}
    assert set(rows) == {"appr-confirm", *ids}
    assert rows["appr-confirm"]["card"] == confirm
    mark = gate.common.QUEUE_CUT_MARK
    for gate_id in ids:
        card = rows[gate_id]["card"]
        assert card["reason"].startswith("Merge gate: ") and card["reason"].endswith(mark)
        assert len(json.dumps(card["reason"])) <= gate.common.QUEUE_REASON_BYTES
        assert "changed_files" not in card and card["layer"] == "mergegate"
        assert rows[gate_id]["head"] and rows[gate_id]["base"] == "main"
    # The detail view still answers with the whole card.
    stored = json.loads(_row(gate, ids[0])["card"])
    detail = _detail(gate, ids[0])
    assert detail["card"]["reason"] == stored["reason"]
    assert detail["card"]["changed_files"] == stored["changed_files"]
    assert len(json.dumps(detail["card"])) > 250_000


def test_the_queue_body_cap_leaves_out_gate_rows_only(gate, monkeypatch, capsys):
    now = int(time.time())
    confirm = {"reason": "x" * 5000, "tool": "write_file"}
    gate.table.items["appr-confirm"] = {"approval_id": "appr-confirm", "status": "PENDING",
                                        "lane": "approval", "created_ts": now - 60,
                                        "card": json.dumps(confirm)}
    for i in range(5):
        _post(gate, _body([_change("infra/main.tf")], sha=format(i + 1, "040x")))
    _, full = _queue(gate)
    assert "gate_rows_omitted" not in full and len(full["approvals"]) == 6
    kept = full["approvals"][:3]
    cap = len(json.dumps({"approvals": kept})) + gate.api.QUEUE_COUNT_BYTES
    monkeypatch.setattr(gate.api, "QUEUE_MAX_BYTES", cap)
    out, body = _queue(gate)
    assert len(out["body"]) <= cap
    assert body["approvals"] == kept and body["gate_rows_omitted"] == 3
    assert [row["lane"] for row in kept] == ["approval", "gate", "gate"]
    assert '"queue_gate_rows_omitted": 3' in capsys.readouterr().out
    # Even a cap below the approval-lane row alone never leaves it out.
    monkeypatch.setattr(gate.api, "QUEUE_MAX_BYTES", 100)
    _, body = _queue(gate)
    assert [row["approval_id"] for row in body["approvals"]] == ["appr-confirm"]
    assert body["approvals"][0]["card"] == confirm and body["gate_rows_omitted"] == 5


def test_a_queue_card_keeps_its_first_line_and_counts_stored_bytes(gate):
    common = gate.common
    small = {"reason": "Merge gate: a\nb", "tool": "merge_gate", "changed_files": ["a"],
             "prior_steps": []}
    assert common.queue_card(small) == {"reason": "Merge gate: a\nb", "tool": "merge_gate"}
    # 2 KB of astral characters is about 24 KB as stored, so it is cut.
    emoji = "\U0001F600"
    card = {"reason": "Merge gate: x\n" + "\n".join(emoji * 40 for _ in range(10))}
    cut = common.queue_card(card)["reason"]
    assert len(json.dumps(cut)) <= common.QUEUE_REASON_BYTES
    assert cut.startswith("Merge gate: x\n") and cut.endswith(common.QUEUE_CUT_MARK)
    assert all(line == emoji * 40 for line in cut.split("\n")[1:-1])
    # A first line alone over the budget is cut by characters, never dropped.
    alone = common.queue_card({"reason": "Merge gate: " + emoji * 1000})["reason"]
    assert alone.startswith("Merge gate: " + emoji) and alone.endswith(common.QUEUE_CUT_MARK)
    assert len(json.dumps(alone)) <= common.QUEUE_REASON_BYTES


def test_a_gate_token_cannot_use_the_approver_routes(gate):
    _, view = _post(gate, _body([_change("infra/main.tf")]))
    out = gate.api.handler(
        _event("POST /approvals/{id}/decision", _claims(), {"decision": "approve"},
               {"id": view["gate_id"]}), None)
    assert out["statusCode"] == 403 and _row(gate, view["gate_id"])["status"] == "PENDING"


# --- a person decides -------------------------------------------------------------------


@pytest.mark.parametrize(("decision", "final"), [("approve", "APPROVED"), ("decline", "DECLINED")])
def test_an_approver_decides_a_gate_run_and_no_agent_is_resumed(gate, decision, final):
    _, view = _post(gate, _body([_change("infra/main.tf")]))
    gate_id = view["gate_id"]
    out = gate.api.handler(
        _event("POST /approvals/{id}/decision", APPROVER, {"decision": decision}, {"id": gate_id}),
        None)
    assert out["statusCode"] == 202 and json.loads(out["body"])["status"] == final
    assert gate.runner_calls == [] and gate.agent_calls == []
    assert _row(gate, gate_id)["decided_by_name"] == "owner"
    assert _get(gate, gate_id)[1]["status"] == final
    assert _event_types(gate, gate_id)[-1] == "DecisionRecorded"
    again = gate.api.handler(
        _event("POST /approvals/{id}/decision", APPROVER, {"decision": "approve"}, {"id": gate_id}),
        None)
    assert again["statusCode"] == 409
    # The runner refuses to resume it, even when handed one.
    assert gate.runner.resume(gate_id)["status"] == "noop" and gate.agent_calls == []


def test_a_gate_run_that_passed_cannot_be_decided(gate):
    _, view = _post(gate, _body())
    out = gate.api.handler(
        _event("POST /approvals/{id}/decision", APPROVER, {"decision": "approve"},
               {"id": view["gate_id"]}), None)
    assert out["statusCode"] == 409


def test_the_sweeper_expires_a_gate_run_and_never_resumes_one(gate, monkeypatch):
    _, held = _post(gate, _body([_change("infra/main.tf")]))
    _, decided = _post(gate, _body([_change("infra/main.tf")], sha="c" * 40))
    gate.api.handler(_event("POST /approvals/{id}/decision", APPROVER, {"decision": "approve"},
                            {"id": decided["gate_id"]}), None)
    past = int(time.time()) - 10
    gate.table.items[held["gate_id"]]["expires_ts"] = past
    gate.table.items[decided["gate_id"]]["decided_at"] = "2020-01-01T00:00:00+00:00"
    out = gate.runner.sweep(None)
    assert out["closed_gate"] == 1 and out["redispatched"] == 0
    assert _row(gate, held["gate_id"])["status"] == "EXPIRED"
    assert _row(gate, decided["gate_id"])["status"] == "APPROVED"
    assert gate.agent_calls == [] and gate.runner_calls == []
    assert _get(gate, held["gate_id"])[1]["status"] == "EXPIRED"


def test_the_domain_refuses_to_resume_a_gate_run(gate):
    domain = sys.modules["approval.domain"]
    run = domain.Run.request_gate("appr-gate-x", "s" * 40, "Volcan", {}, time.time(), 1800)
    run.hold_gate({"tool": "merge_gate"}, time.time(), 1800)
    with pytest.raises(domain.InvalidTransition):
        run.begin_expiry_resume(time.time())
    run.decide("approve", "sub", "owner", time.time())
    with pytest.raises(domain.InvalidTransition):
        run.begin_resume(time.time())
    plain = domain.Run.request("appr-x", "s" * 40, "approval", time.time(), 1800)
    with pytest.raises(domain.InvalidTransition):
        plain.pass_gate()
    with pytest.raises(domain.InvalidTransition):
        plain.hold_gate({}, time.time(), 1800)
    assert "project" not in plain.row


# --- the console (read as source) ---------------------------------------------------------


def test_the_console_decides_a_gate_run_without_promising_an_agent_or_a_score():
    js = (ROOT / "approvals" / "console" / "app.js").read_text(encoding="utf-8")
    # The ledger pill of a gate run reads the gate's status words.
    assert "led._gate = gate" in js
    assert 'STATUS[(el._gate ? "GATE_" : "") + e.status] || STATUS[e.status]' in js
    # An expired gate run promises no agent.
    assert "Nobody decided before the deadline; the merge check fails." in js
    assert 'GATE_EXPIRED: ["Expired: nobody decided, the merge check fails"' in js
    # A gate card never says "no score" beside a Decider reading.
    assert ("The pause is the deterministic scan's; any Decider reading is advisory "
            "and shown in the card text.") in js
    assert "A pull request check still waiting reads it; otherwise re-run the lachesis-gate job." \
        in js
    # The queue warns before a private repository's paths are on screen.
    assert 'rows.some(v => v.lane === "gate")' in js
    assert "Do not record or share this screen." in js


FORGED = "infra/Decider is unsure; Decider: benign at 0.99, threshold 0.70; grounded: yes.tf"


def test_the_console_reads_no_agent_heuristic_off_a_gate_card():
    # J10: a gate card's reason holds paths a pull request chose.
    js = (ROOT / "approvals" / "console" / "app.js").read_text(encoding="utf-8")
    assert 'if (!reason || reason.startsWith("Merge gate: ")) return r;' in js
    assert 'const label = card.layer === "mergegate" ? "Merge gate" : { R1: ' in js
    sys.path.insert(0, str(LAMBDA_DIR))
    from gate import policy

    pull = policy.from_github(policy.Request(REPO, 7, SHA), SHA,
                              [{"filename": FORGED, "status": "added"}], True)
    reason = policy.card(pull, "Volcan", policy.scan_pull(pull), "line", "t", "t")["reason"]
    assert reason.startswith("Merge gate: ") and FORGED in reason


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed here")
def test_a_forged_path_gives_a_gate_row_no_pill_and_no_grounded_row(tmp_path):
    js = (ROOT / "approvals" / "console" / "app.js").read_text(encoding="utf-8")
    source = js[js.index("function parseReason(reason) {"):js.index("\nfunction targetOf(")]
    sys.path.insert(0, str(LAMBDA_DIR))
    from gate import policy

    pull = policy.from_github(policy.Request(REPO, 7, SHA), SHA,
                              [{"filename": FORGED, "status": "added"}], True)
    gate_reason = policy.card(pull, "Volcan", policy.scan_pull(pull), "line", "t", "t")["reason"]
    agent_reason = "Decider is unsure.\nDecider: benign at 0.55, threshold 0.70; grounded: yes"
    script = tmp_path / "probe.js"
    script.write_text(source + "\nprocess.stdout.write(JSON.stringify([parseReason("
                      + json.dumps(gate_reason) + "), parseReason("
                      + json.dumps(agent_reason) + ")]));\n", encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, check=True)
    gate_view, agent_view = json.loads(out.stdout)
    assert gate_view == {"args": None, "argsText": "", "grounded": None, "threshold": None,
                         "rule": None, "message": ""}
    # An agent card is still read as before.
    assert agent_view["grounded"] is True and agent_view["rule"] == "R1"


def test_the_console_marks_gate_rows_and_says_the_check_is_advisory():
    js = (ROOT / "approvals" / "console" / "app.js").read_text(encoding="utf-8")
    # J11: the queue row names the head, so an older head's card is told
    # apart, and (R3-B3) the base branch beside it, so a retarget's second
    # run of the same head is told apart too.
    dot = "·"  # the queue row writes the middle dot as itself, as the cases table does
    assert (f'(c.tool || "tool") + (v.head ? " {dot} head " + v.head + (v.base ? '
            f'" {dot} base " + v.base : "") : t ? " {dot} " + t : "")') in js
    assert f'h("td", {{ "data-label": "Tool {dot} target" }},\n' in js.replace("\r\n", "\n")
    # J13: the detail view warns too, appended only for a gate run.
    assert ('if (card.tool || card.reason) { if (gate) body.append(h("p", { class: "lane-warn", '
            'text: "This merge-gate run can name a private repository and its file paths. '
            'Do not record or share this screen." })); body.append(decisionCard(v, gate ? '
            '"gate" : "approval", () => ctl.kick())); }') in js
    # J14: the queue's Score cell of a gate row never says "no score".
    assert 'text: v.lane === "gate" ? "scan hold" : "no score"' in js
    # J15: the check is advisory, said where the approver decides.
    assert ("any Decider reading is advisory and shown in the card text. The pull request "
            "check itself is advisory: it is not yet required, so it blocks no merge.") in js


def test_the_gate_package_claims_no_more_than_decider_does():
    # J16: decider.py qualifies the claim with the scale-in race.
    doc = " ".join((LAMBDA_DIR / "gate" / "__init__.py").read_text(encoding="utf-8").split())
    assert "never invokes a component without a copy" not in doc
    assert "a narrow race with scale-in remains, see decider.py" in doc


# --- the stack and the workflow (read as source) ------------------------------------------


STACK = ROOT / "infra" / "stacks" / "approvals.py"
WORKFLOW = ROOT / ".github" / "workflows" / "lachesis-gate.yml"


def test_the_stack_wires_the_gate_with_least_privilege():
    source = STACK.read_text(encoding="utf-8")
    constants = {
        node.targets[0].id: node.value.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
    }
    assert constants["GITHUB_OIDC_ISSUER"] == GITHUB
    assert constants["GATE_AUDIENCE"] == "lachesis-gate"
    assert constants["DECIDER_ENDPOINT_NAME"] == "lachesis-decider"
    assert constants["DECIDER_COMPONENT_NAME"] == "lachesis-decider-2b"
    gate = source[source.index('"Gate",'):source.index("stage = apigw.CfnStage(")]
    assert 'handler="gate_api.handler"' in gate
    assert 'grant_store(gate_fn, ("GetItem", "PutItem", "UpdateItem"))' in gate
    assert re.findall(r'actions=\[([^\]]*)\]', gate) == [
        '"secretsmanager:GetSecretValue"', '"sagemaker:DescribeInferenceComponent"',
        '"sagemaker:InvokeEndpoint"']
    assert "resources=[github_secret_arn]" in gate
    assert '"GITHUB_SECRET_ID": GITHUB_SECRET_NAME' in gate
    assert "resources=[decider_component_arn]" in gate
    assert 'f"endpoint/{DECIDER_ENDPOINT_NAME}"' in gate
    assert 'f"inference-component/{DECIDER_COMPONENT_NAME}"' in gate
    assert "*" not in "".join(re.findall(r"resources=\[[^\]]*\]", gate))
    assert "issuer=GITHUB_OIDC_ISSUER" in gate and "audience=[GATE_AUDIENCE]" in gate
    assert 'gate_routes = ("POST /gate/pulls", "GET /gate/{id}")' in gate
    assert "authorizer_id=gate_authorizer.attr_authorizer_id" in gate
    assert 'authorization_type="JWT"' in gate
    # Only the gate function holds SageMaker grants.
    assert source.count('"sagemaker:') == 2
    gate_source = (LAMBDA_DIR / "gate_api.py").read_text(encoding="utf-8")
    for route in ("POST /gate/pulls", "GET /gate/{id}"):
        assert f'route == "{route}"' in gate_source
    settings = source[source.index('"RouteSettings",'):source.index("for route_resource in")]
    assert '"POST /gate/pulls": {"ThrottlingRateLimit": 2, "ThrottlingBurstLimit": 5}' in settings


def _workflow():
    import yaml

    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_names_the_pull_request_and_sends_no_file_list():
    text = WORKFLOW.read_text(encoding="utf-8")
    doc = _workflow()
    assert set(doc[True]) == {"pull_request"}  # "on" parses as True
    assert doc["permissions"] == {"id-token": "write", "contents": "read"}
    job = doc["jobs"]["gate"]
    assert job["continue-on-error"] is True
    assert "NOT YET A REQUIRED CHECK, AND NOT YET ENFORCING" in text
    assert "must never block other work" in text and "can skip the call" in text
    assert "vars.LACHESIS_GATE_URL != ''" in job["if"]
    assert "head.repo.full_name == github.repository" in job["if"]
    steps = "\n".join(step["run"] for step in job["steps"])
    assert "${{" not in steps
    # No file list leaves the job: the gate reads GitHub itself.
    assert "gh api" not in steps and "patch" not in steps and "changes" not in steps
    assert "'{repository: $repo, pr_number: $pr, head_sha: $sha}'" in steps
    assert "&audience=lachesis-gate" in steps and 'echo "::add-mask::${TOKEN}"' in steps
    assert "call POST /gate/pulls gate-request.json" in steps
    assert "429|503|000) ;;" in steps
    assert 'call GET "/gate/${gate_id}"' in steps
    assert "sleep 15" in steps and "+ 600 ))" in steps
    assert "PASSED|APPROVED) report \"$1\"; exit 0" in steps
    assert "DECLINED|EXPIRED|ERROR) report \"$1\"; exit 1" in steps
    assert "Advisory only: this is not yet a required or enforcing check." in steps
    # The token is fetched in the step's own shell, never in $( ), so the
    # mask command is not swallowed.
    assert "$(call" not in steps and "$(fresh_token" not in steps
    # The file the gate pins in the token's job_workflow_ref is this one.
    sys.path.insert(0, str(LAMBDA_DIR))
    assert WORKFLOW.relative_to(ROOT).as_posix() == WORKFLOW_FILE


def test_a_retarget_runs_the_check_again():
    # J1: GitHub sends "edited" when the base branch changes.
    types = _workflow()[True]["pull_request"]["types"]
    assert "edited" in types
    assert {"opened", "synchronize", "reopened", "ready_for_review"} <= set(types)


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed here")
def test_the_workflows_body_is_what_the_gate_parses(tmp_path):
    steps = "\n".join(step["run"] for step in _workflow()["jobs"]["gate"]["steps"])
    program = re.search(r"jq -n .*?\\\n\s*'([^']*)'", steps, re.S).group(1)
    out = subprocess.run(
        ["jq", "-n", "--arg", "repo", REPO, "--argjson", "pr", "7", "--arg", "sha", SHA,
         program], capture_output=True, text=True, check=True).stdout
    sys.path.insert(0, str(LAMBDA_DIR))
    from gate import policy

    body = json.loads(out)
    assert policy.parse(body) == policy.Request(REPO, 7, SHA)
    assert len(out.encode()) < policy.MAX_BODY_BYTES


def _retry_segment():
    steps = "\n".join(step["run"] for step in _workflow()["jobs"]["gate"]["steps"])
    return steps[steps.index("# retry-begin"):steps.index("# retry-end")]


def test_the_workflow_asks_again_until_a_deadline_and_says_the_throttle_is_shared():
    # R3-B2 (J6): no fixed list of five waits (about 110 s in all).
    text = WORKFLOW.read_text(encoding="utf-8")
    job = _workflow()["jobs"]["gate"]
    segment = _retry_segment()
    assert "for wait in" not in text
    assert "post_deadline=$(( $(date +%s) + 600 ))" in segment
    assert "RANDOM %" in segment and "> 60 ? 60 :" in segment
    assert "pause=$(( pause > left ? left : pause ))" in segment
    # Ten minutes of asking, then ten of polling, inside the job's timeout.
    assert job["timeout-minutes"] == 25
    header = " ".join(line.lstrip("# ") for line in text.split("\non:")[0].splitlines())
    assert ("while the gate is advisory an outside caller can still exhaust the shared route "
            "throttle and make this job fail.") in header


_FAKE_CLOCK = """set -euo pipefail
CLOCK=1000000
TRIES=0
SLEEPS=""
ANSWERS=(%s)
date() { echo "$CLOCK"; }
sleep() { SLEEPS="$SLEEPS $1"; CLOCK=$(( CLOCK + $1 )); }
call() { CODE="${ANSWERS[$TRIES]:-429}"; TRIES=$(( TRIES + 1 )); }
%s
echo "RESULT $CODE $TRIES $(( CLOCK - 1000000 ))$SLEEPS"
"""


def _run_retry(answers):
    script = _FAKE_CLOCK % (" ".join(answers), _retry_segment())
    out = subprocess.run([shutil.which("bash"), "-s"], input=script.encode(),
                         capture_output=True, check=True).stdout.decode()
    code, tries, spent, *sleeps = out.strip().splitlines()[-1].split()[1:]
    return code, int(tries), int(spent), [int(s) for s in sleeps]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed here")
def test_a_throttled_post_is_asked_again_with_jitter_until_the_deadline():
    for _ in range(3):  # the waits are random: run it more than once
        code, tries, spent, sleeps = _run_retry([])
        assert code == "429" and spent == 600 and tries == len(sleeps) + 1
        assert tries > 5 and all(1 <= s <= 60 for s in sleeps)
        assert 2 <= sleeps[0] <= 5 and max(sleeps) > 15
    # The first answer that is not 429, 503 or no answer ends it.
    assert _run_retry(["503", "000", "429", "202"])[:2] == ("202", 4)
    assert _run_retry(["403"]) == ("403", 1, 0, [])

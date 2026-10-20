"""The merge gate (B7b), offline: GitHub OIDC claim checks in the Lambda,
the deterministic scan to the gate status, the cold-GPU path that never
invokes Decider, lane isolation, and a person's decision on a gate run.

DynamoDB is the in-memory fake of tests/test_approvals.py; SageMaker is
a fake that records every call. No network.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import io
import json
import re
import sys
import time
from pathlib import Path

import pytest
from test_approvals import APPROVER, LAMBDA_DIR, PLAIN_USER, _event, modules  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
REPO = "upgradedev/volcan_amazon"
OTHER_REPO = "upgradedev/mercury_amazon"
SHA = "a" * 40
GITHUB = "https://token.actions.githubusercontent.com"


def _claims(repository=REPO, iss=GITHUB, aud="lachesis-gate", event="pull_request",
            ref="refs/pull/7/merge"):
    return {"iss": iss, "aud": aud, "repository": repository, "event_name": event, "ref": ref,
            "sub": f"repo:{repository}:pull_request"}


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


class FakeSageMaker:
    """DescribeInferenceComponent and InvokeEndpoint, recorded."""

    def __init__(self, copies=0, status="InService", answer=None, describe_error=None):
        self.copies, self.status = copies, status
        self.answer = answer or {"choice": "destructive_write", "confidence": 0.93}
        self.describe_error = describe_error
        self.described, self.invoked = [], []

    def describe_inference_component(self, **kwargs):
        self.described.append(kwargs)
        if self.describe_error:
            raise self.describe_error
        return {"InferenceComponentStatus": self.status,
                "RuntimeConfig": {"DesiredCopyCount": 1, "CurrentCopyCount": self.copies}}

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
    sagemaker = FakeSageMaker()
    monkeypatch.setattr(gate_api.decider, "_clients", lambda: (sagemaker, sagemaker))
    agent_calls, runner_calls = [], []
    monkeypatch.setattr(common, "invoke_agent", lambda *a, **k: agent_calls.append(a) or {})
    monkeypatch.setattr(api, "_invoke_runner", lambda *a, **k: runner_calls.append(a) or {})
    monkeypatch.setattr(runner, "_invoke_runner", lambda payload: runner_calls.append(payload))

    class World:
        pass

    world = World()
    world.common, world.api, world.runner, world.table = common, api, runner, table
    world.gate_api, world.sagemaker = gate_api, sagemaker
    world.agent_calls, world.runner_calls = agent_calls, runner_calls
    return world


def _context(claims):
    return {"authorizer": {"jwt": {"claims": _claims() if claims is None else claims}}}


def _post(world, body, claims=None, raw=None, b64=False):
    text = raw if raw is not None else json.dumps(body)
    event = {"routeKey": "POST /gate/pulls", "body": text, "requestContext": _context(claims)}
    if b64:
        event["body"], event["isBase64Encoded"] = base64.b64encode(text.encode()).decode(), True
    out = world.gate_api.handler(event, None)
    return out["statusCode"], json.loads(out["body"])


def _get(world, gate_id, claims=None):
    event = {"routeKey": "GET /gate/{id}", "pathParameters": {"id": gate_id},
             "requestContext": _context(claims)}
    out = world.gate_api.handler(event, None)
    return out["statusCode"], json.loads(out["body"])


def _row(world, gate_id):
    return world.table.items[gate_id]


def _event_types(world, gate_id):
    return [row["event_type"] for row in world.table.dynamo.events_of(gate_id)]


# --- the authorizer's claims, checked again in the Lambda -------------------------


@pytest.mark.parametrize(
    "claims",
    [
        {},
        _claims(iss="https://cognito-idp.eu-west-1.amazonaws.com/pool"),
        _claims(aud="sts.amazonaws.com"),
        _claims(repository="someone/else"),
        _claims(repository="upgradedev/lachesis-guard/../x"),
        {"cognito:groups": "approvers", "sub": "s"},
    ],
)
def test_only_a_github_token_of_a_registered_repository_gets_in(gate, claims):
    assert _post(gate, _body(), claims)[0] == 403
    assert _get(gate, "appr-gate-" + "0" * 40 + "-0", claims)[0] == 403
    assert gate.table.items == {} and gate.sagemaker.described == []


def test_every_registered_repository_is_accepted_and_an_array_audience_too(gate):
    registry = json.loads((LAMBDA_DIR / "projects.json").read_text(encoding="utf-8"))
    for project in registry["projects"]:
        for repo in project["repos"]:
            claims = _claims(repository=repo, aud="[lachesis-gate]")
            status, view = _post(gate, _body(repository=repo), claims)
            assert status == 202 and view["status"] == "PASSED", repo
            assert _row(gate, view["gate_id"])["project"] == project["name"]


@pytest.mark.parametrize(
    "claims",
    [
        _claims(event="push", ref="refs/heads/main"),
        _claims(event="workflow_dispatch", ref="refs/pull/7/merge"),
        _claims(ref="refs/pull/8/merge"),
        _claims(ref="refs/heads/feature"),
        {key: value for key, value in _claims().items() if key != "ref"},
    ],
)
def test_only_a_run_of_this_very_pull_request_may_post_its_gate(gate, claims):
    # Another workflow of the same repository (a push, a dispatch, another
    # pull request's run) can neither open nor pre-empt this head's run.
    status, body = _post(gate, _body([]), claims)
    assert status == 403 and "not from a run of this pull request" in body["error"]
    assert gate.table.items == {}
    assert _post(gate, _body([]))[0] == 202


def test_a_long_path_is_cut_and_flagged_never_refused(gate):
    long_path = "src/" + "d" * 500 + ".py"
    status, view = _post(gate, _body([_change(long_path)]))
    assert status == 202 and view["status"] == "PENDING"
    card = json.loads(_row(gate, view["gate_id"])["card"])
    assert "the path was longer than 400 characters and was cut" in card["reason"]
    assert long_path not in card["reason"]


def test_the_body_must_name_the_tokens_own_repository(gate):
    status, body = _post(gate, _body(repository=OTHER_REPO))
    assert status == 403 and "another repository" in body["error"]
    assert gate.table.items == {}


# --- the request contract ------------------------------------------------------


def test_the_body_and_the_file_list_are_capped_before_any_scan(gate):
    policy = gate.gate_api.policy
    big = _body([_change(excerpt="x" * 7000) for _ in range(80)])
    assert len(json.dumps(big)) > policy.MAX_BODY_BYTES
    assert _post(gate, big)[0] == 413
    many = _body([_change(path=f"f{i}.txt") for i in range(policy.MAX_FILES + 1)])
    assert _post(gate, many)[0] == 413
    assert gate.table.items == {}


@pytest.mark.parametrize(
    "body",
    [
        _body(sha="ABC"),
        _body(sha="A" * 40),
        {**_body(), "pr_number": True},
        {**_body(), "pr_number": 0},
        {**_body(), "changes": "nope"},
        _body([{"path": "a", "status": "deleted"}]),
        _body([{"path": "", "status": "added"}]),
        _body([{"path": "a\x00b", "status": "added"}]),
        _body([{"path": "a", "status": "added", "additions": -1}]),
        _body([{"path": "a", "status": "added", "added_lines_excerpt": 5}]),
        _body([{"path": "a", "status": "added", "excerpt_truncated": "yes"}]),
        {**_body(), "files_truncated": 1},
    ],
)
def test_a_malformed_body_is_400_and_writes_nothing(gate, body):
    assert _post(gate, body)[0] == 400
    assert gate.table.items == {}


def test_a_body_that_is_not_json_is_400_and_base64_bodies_are_read(gate):
    assert _post(gate, None, raw="{not json")[0] == 400
    assert _post(gate, None, raw="[]")[0] == 400
    status, view = _post(gate, _body(), b64=True)
    assert status == 202 and view["status"] == "PASSED"


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
    # One transaction: the row and both events, so it is never seen STARTING.
    assert len(gate.table.dynamo.transactions) == 1
    assert _event_types(gate, view["gate_id"]) == ["RunRequested", "RunCompleted"]
    assert gate.sagemaker.described == [] and gate.sagemaker.invoked == []


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (_change(".github/workflows/ci.yml"), "workflow_changed"),
        (_change("infra/stacks/app.py"), "infrastructure_changed"),
        (_change("db/migrations/0002.sql"), "database_migration"),
        (_change("src/app.py", "removed", "", additions=0, deletions=40), "file_deleted"),
        (_change("schema.sql", excerpt="DROP TABLE users;"), "database_migration"),
        (_change("conf.py", excerpt="key = 'AKIA" + "Q" * 16 + "'"), "secret_in_added_line"),
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
    "body",
    [
        _body([_change(excerpt="ok", excerpt_truncated=True)]),
        _body([_change(excerpt="x\n" * 4500)]),
        _body([_change(excerpt="")]),
        _body(files_truncated=True),
    ],
)
def test_what_was_not_read_is_never_called_clean(gate, body):
    status, view = _post(gate, body)
    assert status == 202 and view["status"] == "PENDING"
    assert view["summary"]["files_by_rule"]["scan_incomplete"] == 1


def test_only_the_file_list_cut_does_not_describe_the_cold_gpu(gate):
    status, view = _post(gate, _body(files_truncated=True))
    card = json.loads(_row(gate, view["gate_id"])["card"])
    assert "(files not sent)" in card["reason"] and view["summary"]["decider"] == "not_asked"
    assert gate.sagemaker.described == []


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
    card = json.loads(_row(gate, view["gate_id"])["card"])
    assert "... and 5 more flagged files." in card["reason"] and len(card["changed_files"]) == 20
    assert view["summary"]["flagged_files"] == 25


# --- Decider: never woken, never the gate ----------------------------------------


@pytest.mark.parametrize(
    "sagemaker",
    [
        FakeSageMaker(copies=0),
        FakeSageMaker(copies=1, status="Updating"),
        FakeSageMaker(copies=None),
        FakeSageMaker(describe_error=RuntimeError("AccessDenied")),
    ],
)
def test_a_cold_gpu_is_never_invoked_and_a_person_decides(gate, monkeypatch, sagemaker):
    monkeypatch.setattr(gate.gate_api.decider, "_clients", lambda: (sagemaker, sagemaker))
    status, view = _post(gate, _body([_change("infra/main.tf")]))
    assert status == 202 and view["status"] == "PENDING"
    assert len(sagemaker.described) == 1 and sagemaker.invoked == []
    assert sagemaker.described[0] == {"InferenceComponentName": "lachesis-decider-2b"}
    card = json.loads(_row(gate, view["gate_id"])["card"])
    assert "Decider asleep: a person decides" in card["reason"]
    assert view["summary"]["decider"] == "asleep"


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
    card = json.loads(_row(gate, view["gate_id"])["card"])
    line = (f"Strands Decider 2B, advisory: {answer['choice']} "
            f"at {answer['confidence']:.2f}, 120 ms.")
    assert line in card["reason"] and "It does not clear a flag" in card["reason"]
    assert card["confidence"] is None and card["layer"] == "mergegate"
    assert view["summary"]["decider"] == "answered"


@pytest.mark.parametrize("answer", [
    {"choice": "harmless", "confidence": 0.9},
    {"choice": "benign", "confidence": 1.5},
    {"choice": "benign", "confidence": float("nan")},
    {"choice": "benign"},
])
def test_an_unusable_decider_answer_is_a_failure_not_a_score(gate, answer):
    gate.sagemaker.copies, gate.sagemaker.answer = 1, answer
    status, view = _post(gate, _body([_change("infra/main.tf")]))
    card = json.loads(_row(gate, view["gate_id"])["card"])
    assert view["summary"]["decider"] == "failed" and "Decider did not answer" in card["reason"]


def test_the_risk_question_is_the_patterns_question():
    sys.path.insert(0, str(LAMBDA_DIR))
    from gate import decider

    from lachesis import questions

    assert decider.risk_question() == questions.risk_question()
    assert decider.RISK_CLASSES == questions.RISK_CLASSES


# --- one run per pull-request head --------------------------------------------------


def test_a_rerun_finds_the_same_run_and_a_closed_one_opens_the_next_round(gate):
    gate.sagemaker.copies = 1
    first = _post(gate, _body([_change("infra/main.tf")]))
    again = _post(gate, _body([_change("infra/main.tf")]))
    assert first[0] == 202 and again[0] == 200 and again[1]["gate_id"] == first[1]["gate_id"]
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
        gate.table.items[policy.gate_id(pull, round_)] = {
            "approval_id": policy.gate_id(pull, round_), "status": "ERROR", "lane": "gate"}
    assert _post(gate, _body())[0] == 409


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
    assert 'grant_store(gate_fn, ("GetItem", "PutItem"))' in gate
    assert re.findall(r'actions=\[([^\]]*)\]', gate) == [
        '"sagemaker:DescribeInferenceComponent"', '"sagemaker:InvokeEndpoint"']
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


def test_the_workflow_asks_the_gate_without_blocking_anything_yet():
    text = WORKFLOW.read_text(encoding="utf-8")
    import yaml

    doc = yaml.safe_load(text)
    assert set(doc[True]) == {"pull_request"}  # "on" parses as True
    assert doc["permissions"] == {"id-token": "write", "contents": "read",
                                  "pull-requests": "read"}
    job = doc["jobs"]["gate"]
    assert job["continue-on-error"] is True
    assert "NOT YET A REQUIRED CHECK" in text and "must never block other work" in text
    assert "vars.LACHESIS_GATE_URL != ''" in job["if"]
    assert "head.repo.full_name == github.repository" in job["if"]
    steps = "\n".join(step["run"] for step in job["steps"])
    assert "${{" not in steps
    assert 'gh api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/files' in steps
    assert "&audience=lachesis-gate" in steps and 'echo "::add-mask::${TOKEN}"' in steps
    assert "call POST /gate/pulls gate-request.json" in steps
    assert 'call GET "/gate/${gate_id}"' in steps
    assert "sleep 15" in steps and "+ 600 ))" in steps
    assert "PASSED|APPROVED) report \"$1\"; exit 0" in steps
    assert "DECLINED|EXPIRED|ERROR) report \"$1\"; exit 1" in steps
    # The token is fetched in the step's own shell, never in $( ), so the
    # mask command is not swallowed.
    assert "$(call" not in steps and "$(fresh_token" not in steps
    assert job["env"]["MAX_FILES"] == "300" and job["env"]["MAX_EXCERPT_CHARS"] == "8000"


def test_the_workflow_caps_are_the_gates_caps():
    import yaml

    sys.path.insert(0, str(LAMBDA_DIR))
    from gate import policy

    env = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["gate"]["env"]
    from activity import scan

    assert int(env["MAX_FILES"]) == policy.MAX_FILES
    assert int(env["MAX_EXCERPT_CHARS"]) == policy.MAX_EXCERPT_CHARS
    # The added lines in all fit the scan budget and leave room under the
    # body cap; the byte backstop sits below the cap the gate refuses at.
    total = int(env["MAX_TOTAL_EXCERPT_CHARS"])
    assert total <= scan.SCAN_COMMIT_CHARS and total < policy.MAX_BODY_BYTES * 0.7
    assert int(env["MAX_BODY_BYTES"]) < policy.MAX_BODY_BYTES
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["gate"]
    steps = "\n".join(step["run"] for step in job["steps"])
    assert '-gt "$MAX_BODY_BYTES"' in steps
    assert '.added_lines_excerpt = "" | .excerpt_truncated = true' in steps
    assert "<= $max_total then $cut else" in steps


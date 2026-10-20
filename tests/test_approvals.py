"""Approvals logic tests (B1 acceptance, offline): a repeat decision is
refused, a non-approver gets 403, a duplicate resume resumes once, and
the /try daily counter stops at the cap. DynamoDB is an in-memory fake
implementing exactly the conditional writes common.py uses.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "approvals" / "lambda"
sys.path.insert(0, str(LAMBDA_DIR))


class FakeTable:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803
        key = Item["approval_id"]
        if ConditionExpression and key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
            )
        self.items[key] = dict(Item)
        return {}

    def get_item(self, Key):  # noqa: N803
        item = self.items.get(Key["approval_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None, **kwargs):  # noqa: N803
        key = Key["approval_id"]
        item = self.items.get(key)
        values = kwargs.get("ExpressionAttributeValues", {})
        names = kwargs.get("ExpressionAttributeNames", {})
        if ConditionExpression == "#s = :expected" and (
            item is None or item.get("status") != values[":expected"]
        ):
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
            )
        if item is None:
            item = {"approval_id": key}
            self.items[key] = item
        if "if_not_exists" in UpdateExpression:
            current = int(item.get("count", 0))
            over_cap = "attribute_not_exists(#c) OR #c < :cap" in (ConditionExpression or "")
            if over_cap and current >= int(values[":cap"]):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "UpdateItem",
                )
            item["count"] = current + 1
            if "#t = if_not_exists(#t, :ttl)" in UpdateExpression:
                item.setdefault(names["#t"], values[":ttl"])
            return {"Attributes": dict(item)}
        assignments = UpdateExpression.removeprefix("SET ").split(", ")
        for assignment in assignments:
            lhs, rhs = [part.strip() for part in assignment.split("=", 1)]
            field = names.get(lhs, lhs)
            item[field] = values[rhs]
        return {"Attributes": dict(item)}

    def query(self, IndexName, KeyConditionExpression, **kwargs):  # noqa: N803
        status = kwargs["ExpressionAttributeValues"][":status"]
        rows = [dict(i) for i in self.items.values() if i.get("status") == status]
        rows.sort(key=lambda i: int(i.get("created_ts", 0)), reverse=True)
        return {"Items": rows[: kwargs.get("Limit", 50)]}


@pytest.fixture()
def modules(monkeypatch):
    table = FakeTable()
    spec = importlib.util.spec_from_file_location("common", LAMBDA_DIR / "common.py")
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    monkeypatch.setattr(common, "_table", table)
    for name in ("api", "runner"):
        spec = importlib.util.spec_from_file_location(name, LAMBDA_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "common", common)
    return common, sys.modules["api"], sys.modules["runner"], table


def _event(route, claims=None, body=None, params=None):
    return {
        "routeKey": route,
        "pathParameters": params or {},
        "body": json.dumps(body or {}),
        "requestContext": {"authorizer": {"jwt": {"claims": claims or {}}}},
    }


APPROVER = {"sub": "sub-1", "username": "owner", "cognito:groups": "approvers"}
PLAIN_USER = {"sub": "sub-2", "username": "guest", "cognito:groups": ""}


def test_create_and_transition_guard(modules):
    common, _, _, table = modules
    common.create_row("appr-1", "s" * 40, "approval")
    common.transition("appr-1", "STARTING", "PENDING", interrupts="[]", card="{}")
    moved = common.transition("appr-1", "PENDING", "APPROVED", decided_by="sub-1")
    assert moved["status"] == "APPROVED"
    assert common.transition("appr-1", "PENDING", "DECLINED") is None


def _decide(api, claims, decision, approval_id):
    event = _event(
        "POST /approvals/{id}/decision", claims, {"decision": decision}, {"id": approval_id}
    )
    return api.handler(event, None)


def test_decision_repeat_is_409_and_non_approver_403(modules, monkeypatch):
    common, api, _, _ = modules
    common.create_row("appr-2", "s" * 40, "approval")
    common.transition("appr-2", "STARTING", "PENDING", interrupts="[]", card="{}")
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    assert _decide(api, PLAIN_USER, "approve", "appr-2")["statusCode"] == 403
    assert _decide(api, APPROVER, "approve", "appr-2")["statusCode"] == 202
    assert _decide(api, APPROVER, "decline", "appr-2")["statusCode"] == 409


def test_resume_only_once(modules, monkeypatch):
    common, _, runner, _ = modules
    common.create_row("appr-3", "s" * 40, "approval")
    interrupts = '[{"interruptId": "i1"}]'
    common.transition("appr-3", "STARTING", "PENDING", interrupts=interrupts, card="{}")
    common.transition("appr-3", "PENDING", "APPROVED", decided_by="sub-1")
    calls = []
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda session_id, payload, **kw: calls.append(payload)
        or {"status": "completed", "text": "done"},
    )
    assert runner.resume("appr-3")["status"] == "DONE"
    assert runner.resume("appr-3")["status"] == "noop"
    assert len(calls) == 1


def test_resume_error_before_send_returns_row_to_pending(modules, monkeypatch):
    # B0 item 3: only a failure that provably never reached the agent may
    # go back to PENDING. This test used a RuntimeError before B0, which
    # proves nothing about the send; it now uses a pre-send error.
    common, _, runner, _ = modules
    common.create_row("appr-4", "s" * 40, "approval")
    interrupts = '[{"interruptId": "i1"}]'
    common.transition("appr-4", "STARTING", "PENDING", interrupts=interrupts, card="{}")
    common.transition("appr-4", "PENDING", "APPROVED", decided_by="sub-1")

    def _boom(session_id, payload, **kw):
        raise EndpointConnectionError(endpoint_url="https://agentcore.invalid")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    assert runner.resume("appr-4")["status"] == "error-resumable"
    row = common.get("appr-4")
    assert row["status"] == "PENDING"
    assert row["sweep_attempts"] == 1


def test_start_card_carries_decider_fields(modules, monkeypatch):
    common, _, runner, _ = modules
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda session_id, payload, **kw: {
            "status": "interrupt",
            "interrupts": [{"interruptId": "i1", "reason": "card text"}],
            "triage": [
                {
                    "tool": "write_file",
                    "risk": "benign",
                    "confidence": 0.76,
                    "grounded": True,
                    "latency_ms": 712.0,
                    "layer": "decider",
                    "verdict": "proceed",
                },
                {
                    "tool": "delete_file",
                    "risk": "destructive_write",
                    "confidence": 0.31,
                    "latency_ms": 187.4,
                    "layer": "decider",
                    "verdict": "confirm",
                },
            ],
            "changed_files": ["cleanup_notes.txt"],
        },
    )
    out = runner.start("prompt", "try", "appr-card")
    assert out["status"] == "interrupt"
    import json

    card = json.loads(common.get("appr-card")["card"])
    assert card["risk"] == "destructive_write"
    assert card["confidence"] == 0.31
    assert card["latency_ms"] == 187.4
    assert card["layer"] == "decider"
    assert len(card["prior_steps"]) == 1
    assert card["prior_steps"][0]["tool"] == "write_file"
    assert card["prior_steps"][0]["verdict"] == "proceed"
    assert card["changed_files"] == ["cleanup_notes.txt"]
    assert card["expires_at"]


def test_resume_repause_card_keeps_decider_fields(modules, monkeypatch):
    common, _, runner, _ = modules
    common.create_row("appr-5", "s" * 40, "approval")
    common._table.items["appr-5"]["expires_ts"] = 100
    interrupts = '[{"interruptId": "i1"}]'
    common.transition("appr-5", "STARTING", "PENDING", interrupts=interrupts, card="{}")
    common.transition(
        "appr-5", "PENDING", "APPROVED", decided_by="sub-1", decided_by_name="owner"
    )
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda session_id, payload, **kw: {
            "status": "interrupt",
            "interrupts": [{"interruptId": "i2", "reason": "second pause"}],
            "triage": [
                {
                    "tool": "run_tests",
                    "risk": "destructive_write",
                    "confidence": 0.24,
                    "latency_ms": 171.0,
                    "layer": "decider",
                    "verdict": "confirm",
                }
            ],
        },
    )
    assert runner.resume("appr-5")["status"] == "interrupt-again"
    import json

    row = common.get("appr-5")
    card = json.loads(row["card"])
    assert card["tool"] == "run_tests"
    assert card["risk"] == "destructive_write"
    assert card["confidence"] == 0.24
    assert card["latency_ms"] == 171.0
    assert card["layer"] == "decider"
    assert card["expires_at"]
    assert int(row["expires_ts"]) > 100
    # B0 item 4: a re-pause keeps who decided, and logs the decision.
    assert row["decided_by"] == "sub-1"
    assert row["decided_by_name"] == "owner"
    assert [d["decision"] for d in row["decisions"]] == ["approve"]


def test_empty_try_token_is_404(modules):
    _, api, _, _ = modules
    out = api.handler(_event("GET /try/{token}", {}, None, {"token": ""}), None)
    assert out["statusCode"] == 404


def test_expired_decision_is_409(modules, monkeypatch):
    common, api, _, _ = modules
    common.create_row("appr-old", "s" * 40, "approval")
    common.transition("appr-old", "STARTING", "PENDING", interrupts="[]", card="{}")
    common._table.items["appr-old"]["expires_ts"] = 1
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    out = _decide(api, APPROVER, "approve", "appr-old")
    assert out["statusCode"] == 409


def test_approver_queue_hides_try_rows(modules):
    common, api, _, _ = modules
    common.create_row("appr-q1", "s" * 40, "approval")
    common.transition("appr-q1", "STARTING", "PENDING", interrupts="[]", card="{}")
    common.create_row("appr-q2", "s" * 40, "try")
    common.transition("appr-q2", "STARTING", "PENDING", interrupts="[]", card="{}")
    body = api.handler(_event("GET /approvals", APPROVER), None)
    import json

    ids = [a["approval_id"] for a in json.loads(body["body"])["approvals"]]
    assert ids == ["appr-q1"]


def test_start_failure_lands_in_error(modules, monkeypatch):
    common, _, runner, _ = modules

    def _boom(session_id, payload, **kw):
        raise RuntimeError("agent down")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    out = runner.start("prompt", "try", "appr-err")
    assert out["status"] == "error"
    assert common.get("appr-err")["status"] == "ERROR"


def test_sweeper_closes_stale_starting_and_caps_attempts(modules, monkeypatch):
    common, _, runner, _ = modules
    common.create_row("appr-stale", "s" * 40, "try")
    common._table.items["appr-stale"]["created_ts"] = 1
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: {"status": "completed", "text": ""}
    )
    out = runner.sweep()
    assert out["closed_starting"] == 1
    assert common.get("appr-stale")["status"] == "ERROR"

    row = {"approval_id": "appr-x", "sweep_attempts": 2}
    common.create_row("appr-x", "s" * 40, "approval")
    common.transition("appr-x", "STARTING", "PENDING", interrupts="[]", card="{}")
    common.transition("appr-x", "PENDING", "APPROVED")
    common.transition("appr-x", "APPROVED", "RESUMING")
    assert runner._attempt_failed(row) == "ERROR"
    assert common.get("appr-x")["status"] == "ERROR"


def test_canned_prompt_opens_with_the_dictated_write(modules):
    _, api, _, _ = modules
    prompt = api.CANNED_PROMPT
    assert prompt.index("cleanup_notes.txt") < prompt.index("delete_file")
    assert "Workspace check started: reviewing refunds_prod_config.py status" in prompt
    assert "sk-" not in prompt


def test_sweeper_expires_try_rows_without_waking_the_agent(modules, monkeypatch):
    common, _, runner, _ = modules
    calls = []
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: calls.append(p) or {"status": "completed"}
    )
    for aid, lane in (("appr-try-old", "try"), ("appr-appr-old", "approval")):
        common.create_row(aid, "s" * 40, lane)
        common.transition(aid, "STARTING", "PENDING", interrupts="[]", card="{}")
        common._table.items[aid]["expires_ts"] = 1
    out = runner.sweep()
    assert common.get("appr-try-old")["status"] == "EXPIRED"
    assert out["closed_try"] == 1
    # only the approval-lane row resumed its agent (with the refusal)
    assert len(calls) == 1
    assert common.get("appr-appr-old")["status"] == "EXPIRED"
    assert common.get("appr-appr-old").get("resuming_ts") is not None


def test_send_card_escapes_html_and_skips_when_unset(modules, monkeypatch):
    _, _, runner, _ = modules
    sent = []

    class FakeSes:
        def send_email(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(runner.boto3, "client", lambda service, **kw: FakeSes())
    item = {
        "approval_id": "appr-<script>",
        "card": json.dumps({"reason": "bad <b>tag</b>", "expires_at": "2026-10-17T00:00:00Z"}),
    }
    monkeypatch.setattr(runner, "SES_FROM", "")
    monkeypatch.setattr(runner, "SES_TO", "")
    runner._send_card(item)
    assert sent == []

    monkeypatch.setattr(runner, "SES_FROM", "from@example.com")
    monkeypatch.setattr(runner, "SES_TO", "to@example.com")
    runner._send_card(item)
    assert len(sent) == 1
    html_body = sent[0]["Message"]["Body"]["Html"]["Data"]
    assert "<b>tag</b>" not in html_body
    assert "&lt;b&gt;tag&lt;/b&gt;" in html_body
    assert "&lt;script&gt;" in html_body


CONSOLE_DIR = Path(__file__).resolve().parents[1] / "approvals" / "console"
CONSOLE_FILES = ("index.html", "app.css", "app.js")


def _console(name: str) -> str:
    return (CONSOLE_DIR / name).read_text(encoding="utf-8")


def test_console_split_and_budget():
    # config.js is generated by infra/stacks/approvals.py at deploy time and
    # must never be shadowed by a checked-in file.
    assert not (CONSOLE_DIR / "config.js").exists()
    html = _console("index.html")
    assert '<script src="config.js"></script>' in html
    assert "app.js?v=" in html and "app.css?v=" in html
    assert '<link rel="icon" href="data:,">' in html
    for landmark in ("<header", "<aside", "<main", "<footer", "<dialog", "<form"):
        assert landmark in html
    for view in ("home", "try", "queue", "detail", "policy", "metrics"):
        assert f'data-view="{view}"' in html
    for elem in ('class="sidebar"', 'id="cases-table"', 'id="thr-tabs"', 'id="drawer"'):
        assert elem in html
    total = sum(len(_console(name).splitlines()) for name in CONSOLE_FILES)
    assert total <= 900, total



def test_console_copy_rules():
    for name in CONSOLE_FILES:
        text = _console(name)
        for dash in ("\u2013", "\u2014", "\\u2013", "\\u2014", "&mdash;", "&ndash;"):
            assert dash not in text, (name, dash)
        # Owner decision 2026-10-19: the hosted demo emails nobody, and no
        # console text may say or imply that it does.
        assert "email" not in text.lower(), name
        assert "never the gate" not in text.replace("The model is never the gate", ""), name
    html = _console("index.html")
    assert "Every tool call is scored before it runs." in html
    assert "Decider scores it; if benign and grounded, it runs." in html
    assert (
        "a destructive write always waits for a person, "
        "and so does any call Decider scores under 0.70." in html
    )
    assert "below the 0.70 gate" not in html
    assert "approve or decline the paused call right here" not in html


def test_console_app_rules():
    js = _console("app.js")
    # Server data reaches the page through textContent only.
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js
    assert "NEW_PASSWORD_REQUIRED" in js
    assert '"/try/" + encodeURIComponent(id) + "/decision"' in js
    for label in ("Waiting for a person", "Finished without running the paused call",
                  "Expired: nobody decided, treated as declined", "Run failed"):
        assert label in js
    assert "Could not complete:" in js
    for piece in ("The Decider GPU is asleep",
                  ("It scales to zero when idle. This call paused fail-closed: no score, "
                   "and the public demo cannot decide it. "),
                  ("Your click has asked the GPU to wake; a run started after it is awake "
                   "will be scored. This pause expires at "),
                  " UTC and is then treated as declined; nothing runs."):
        assert piece in js
    assert "15000" in js and "10000" in js
    assert "paste a staging key" not in js


def test_console_reason_parser_matches_confirm_prompt():
    """The console parses the pause reason; pin it to the real format."""
    import re

    from lachesis.handler import _confirm_prompt
    from lachesis.policy import decide

    js = _console("app.js")
    pattern = r"Decider: (\S+) at ([0-9.]+), threshold ([0-9.]+); grounded: (yes|no)"
    assert pattern in js
    assert '"\\narguments:\\n"' in js
    verdict = decide("destructive_write", 0.85, 0.9)
    reason = _confirm_prompt("delete_file", {"path": "refunds_prod_config.py"}, verdict, 0.7)
    match = re.search(pattern, reason)
    assert match and match.group(3) == "0.70" and match.group(4) == "yes"
    assert "\narguments:\n" in reason
    for phrase, message in (
        ("pauses this", verdict.message),
        ("is unsure", decide("benign", 0.5, 0.9).message),
        ("does not know the risk class", decide("odd", 0.9, 0.9).message),
    ):
        assert phrase in message and phrase in js


def test_try_counter_cap(modules, monkeypatch):
    common, api, _, _ = modules
    monkeypatch.setattr(common, "TRY_DAILY_CAP", 2)
    monkeypatch.setattr(common, "TRY_IP_DAILY_CAP", 5)
    event = {"requestContext": {"http": {"sourceIp": "203.0.113.7"}}}
    assert api._try_counter_ok(event) is True
    assert api._try_counter_ok(event) is True
    assert api._try_counter_ok(event) is False


def test_try_per_ip_cap(modules, monkeypatch):
    common, api, _, _ = modules
    monkeypatch.setattr(common, "TRY_DAILY_CAP", 100)
    monkeypatch.setattr(common, "TRY_IP_DAILY_CAP", 1)
    event = {"requestContext": {"http": {"sourceIp": "203.0.113.9"}}}
    assert api._try_counter_ok(event) is True
    assert api._try_counter_ok(event) is False


def test_malformed_json_body_returns_400(modules):
    _, api, _, _ = modules
    for route, params in (
        ("POST /approvals/start", {}),
        ("POST /approvals/{id}/decision", {"id": "appr-1"}),
        ("POST /try/{token}/decision", {"token": "appr-1"}),
    ):
        for bad_body in ("{not-json", "[]", '"string"', "123"):
            ev = {
                "routeKey": route,
                "pathParameters": params,
                "body": bad_body,
                "requestContext": {"authorizer": {"jwt": {"claims": APPROVER}}},
            }
            assert api.handler(ev, None)["statusCode"] == 400


def test_try_decision_and_cross_lane_isolation(modules, monkeypatch):
    common, api, _, _ = modules
    invoked = []
    monkeypatch.setattr(
        api, "_invoke_runner", lambda payload, asynchronous: invoked.append(payload)
    )

    common.create_row("appr-try-warm", "s" * 40, "try")
    common.transition(
        "appr-try-warm",
        "STARTING",
        "PENDING",
        interrupts='[{"interruptId": "i1"}]',
        card=json.dumps({"layer": "decider", "risk": "destructive_write", "confidence": 0.31}),
    )
    # Approver endpoints must not leak or mutate /try rows
    get_ev = _event("GET /approvals/{id}", APPROVER, None, {"id": "appr-try-warm"})
    assert api.handler(get_ev, None)["statusCode"] == 404
    assert _decide(api, APPROVER, "approve", "appr-try-warm")["statusCode"] == 404

    # Public /try/{token}/decision works once on a warm /try row and 409s on repeat
    res1 = api.handler(
        _event(
            "POST /try/{token}/decision",
            {},
            {"decision": "approve"},
            {"token": "appr-try-warm"},
        ),
        None,
    )
    assert res1["statusCode"] == 202
    assert len(invoked) == 1
    res2 = api.handler(
        _event(
            "POST /try/{token}/decision",
            {},
            {"decision": "decline"},
            {"token": "appr-try-warm"},
        ),
        None,
    )
    assert res2["statusCode"] == 409

    # Cold GPU (layer == unavailable) on /try cannot be resumed publicly
    common.create_row("appr-try-cold", "s" * 40, "try")
    common.transition(
        "appr-try-cold",
        "STARTING",
        "PENDING",
        interrupts='[{"interruptId": "i1"}]',
        card=json.dumps({"layer": "unavailable", "confidence": 0.0}),
    )
    cold_res = api.handler(
        _event(
            "POST /try/{token}/decision",
            {},
            {"decision": "approve"},
            {"token": "appr-try-cold"},
        ),
        None,
    )
    assert cold_res["statusCode"] == 409

    # /try/{token}/decision cannot touch an approval-lane row
    common.create_row("appr-real", "s" * 40, "approval")
    common.transition("appr-real", "STARTING", "PENDING", interrupts="[]", card="{}")
    cross = api.handler(
        _event("POST /try/{token}/decision", {}, {"decision": "approve"}, {"token": "appr-real"}),
        None,
    )
    assert cross["statusCode"] == 404


def test_agent_entrypoint_turn_budget_and_workspace_diff(tmp_path, monkeypatch):
    agent_dir = Path(__file__).resolve().parents[1] / "agent"
    sys.path.insert(0, str(agent_dir))
    spec = importlib.util.spec_from_file_location("agent_entry", agent_dir / "entrypoint.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    monkeypatch.setattr(entry, "WORK_ROOT", tmp_path)

    workdir = entry._workdir("s" * 40)
    baseline = entry._snapshot(workdir)
    assert sorted(baseline) == ["refunds_prod_config.py", "test_workspace.py"]
    assert entry._changed_since(workdir, baseline) == []

    (workdir / "cleanup_notes.txt").write_text("note\n", encoding="utf-8")
    assert entry._changed_since(workdir, baseline) == ["cleanup_notes.txt"]

    budget = entry.TurnBudget(max_turns=2)
    budget._count(None)
    budget._count(None)
    with pytest.raises(RuntimeError, match="hosted budget: more than 2 model turns"):
        budget._count(None)


def test_console_motion_respects_reduced_motion_and_never_animates_numbers():
    css = _console("app.css")
    start = css.index("@media (prefers-reduced-motion: no-preference) {\n")
    depth, end = 0, start
    for i in range(start, len(css)):
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        if depth == 0 and css[i] == "}":
            end = i
            break
    outside = css[:start] + css[end:]
    stray = outside.replace("animation-delay", "")
    assert "animation:" not in stray, "animation outside the motion block"
    js = _console("app.js")
    assert "countUp" not in js and "prefers-reduced-motion" in js


# ---------------------------------------------------------------------------
# Slice B0: correctness on the path judges click. One regression test per
# defect, plus the contract reader on its own.
# ---------------------------------------------------------------------------


def _load_contract():
    import contract  # pure module, on sys.path via LAMBDA_DIR

    return contract


INTERRUPT_PAYLOAD = {
    "status": "interrupt",
    "interrupts": [{"interruptId": "i2", "reason": "second pause"}],
    "triage": [{"tool": "run_tests", "verdict": "confirm", "layer": "decider"}],
}


def test_contract_reader_classifies_every_shape():
    contract = _load_contract()
    done = contract.read_result({"status": "completed", "text": "all good", "triage": "x"})
    assert done.kind == contract.COMPLETED and done.text == "all good" and done.triage == []
    paused = contract.read_result(INTERRUPT_PAYLOAD)
    assert paused.kind == contract.INTERRUPT and paused.interrupts[0]["interruptId"] == "i2"
    # The agent's own error bodies come back with HTTP 200 (entrypoint.py).
    for payload, prefix in (
        ({"status": "error", "error": "session_id must be at least 33 characters"}, "AgentError"),
        ({"status": "error"}, "AgentError"),
        ({"status": "weird"}, "UnknownStatus"),
        ({}, "UnknownStatus"),
        (["not", "a", "dict"], "MalformedResult"),
        (None, "MalformedResult"),
        ({"status": "interrupt", "interrupts": []}, "MalformedResult"),
        ({"status": "interrupt", "interrupts": [{"reason": "no id"}]}, "MalformedResult"),
        ({"status": "interrupt", "interrupts": "i1"}, "MalformedResult"),
    ):
        result = contract.read_result(payload)
        assert result.is_error, payload
        assert result.error.split(":", 1)[0] == prefix, (payload, result.error)
    assert contract.read_result({"status": "completed", "text": 7}).text == ""


def test_contract_final_status_needs_a_completed_result():
    contract = _load_contract()
    done = contract.read_result({"status": "completed", "text": ""})
    failed = contract.read_result({"status": "error", "error": "boom"})
    assert contract.final_status(done, "APPROVED") == "DONE"
    assert contract.final_status(done, "DECLINED") == "REFUSED_DONE"
    assert contract.final_status(done, "PENDING") == "ERROR"
    assert contract.final_status(failed, "APPROVED") == "ERROR"
    assert contract.final_status(failed, "DECLINED") == "ERROR"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "error", "error": "prompt or interrupt_responses required"},
        {"status": "mystery"},
        {"status": "interrupt", "interrupts": []},
    ],
)
def test_start_records_agent_error_as_error_not_done(modules, monkeypatch, payload):
    # Regression: runner.start used to record anything but "interrupt" as
    # DONE, so an agent error body (HTTP 200) showed as "Finished".
    common, _, runner, _ = modules
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: payload)
    out = runner.start("prompt", "try", "appr-agent-err")
    assert out["status"] == "error"
    row = common.get("appr-agent-err")
    assert row["status"] == "ERROR"
    assert row["result_text"]


def _approved_row(common, approval_id, status="APPROVED", lane="approval"):
    common.create_row(approval_id, "s" * 40, lane)
    common.transition(
        approval_id, "STARTING", "PENDING", interrupts='[{"interruptId": "i1"}]', card="{}"
    )
    common.transition(
        approval_id,
        "PENDING",
        status,
        decided_by="sub-1",
        decided_by_name="owner",
        decided_at="2026-10-20T10:00:00+00:00",
    )


@pytest.mark.parametrize("decision", ["APPROVED", "DECLINED"])
def test_resume_records_agent_error_as_error(modules, monkeypatch, decision):
    # Regression: runner.resume used to map any non-interrupt answer to
    # DONE or REFUSED_DONE from the decision alone.
    common, _, runner, _ = modules
    _approved_row(common, "appr-r-err", decision)
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: {"status": "error", "error": "agent fell over"}
    )
    assert runner.resume("appr-r-err")["status"] == "ERROR"
    row = common.get("appr-r-err")
    assert row["status"] == "ERROR"
    assert row["result_text"] == "AgentError: agent fell over"


@pytest.mark.parametrize(
    ("decision", "final"), [("APPROVED", "DONE"), ("DECLINED", "REFUSED_DONE")]
)
def test_resume_completed_follows_the_decision(modules, monkeypatch, decision, final):
    common, _, runner, _ = modules
    _approved_row(common, "appr-r-ok", decision)
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: {"status": "completed", "text": "t"}
    )
    assert runner.resume("appr-r-ok") == {"status": final, "text": "t"}
    assert common.get("appr-r-ok")["status"] == final


def _client_error(status: int, code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "InvokeAgentRuntime",
    )


AFTER_SEND_ERRORS = [
    RuntimeError("runtime 500"),
    ReadTimeoutError(endpoint_url="https://agentcore.invalid"),
    ConnectionClosedError(endpoint_url="https://agentcore.invalid"),
    _client_error(424, "RuntimeClientError"),
    _client_error(400, "ValidationException"),
    _client_error(500, "InternalServerException"),
    json.JSONDecodeError("Expecting value", "", 0),
    KeyError("response"),
]


@pytest.mark.parametrize("exc", AFTER_SEND_ERRORS, ids=lambda e: type(e).__name__)
def test_resume_failure_after_send_ends_in_error_never_pending(modules, monkeypatch, exc):
    # Regression: any resume exception used to return the row to PENDING,
    # where a person could decide a call that may already have run.
    common, _, runner, _ = modules
    _approved_row(common, "appr-r-sent")

    def _boom(session_id, payload, **kw):
        raise exc

    monkeypatch.setattr(common, "invoke_agent", _boom)
    out = runner.resume("appr-r-sent")
    assert out == {"status": "ERROR", "error": type(exc).__name__}
    row = common.get("appr-r-sent")
    assert row["status"] == "ERROR"
    assert row["result_text"].startswith(type(exc).__name__ + ":")


def test_failed_before_send_is_exact_class_membership(modules):
    common = modules[0]
    assert common.failed_before_send(EndpointConnectionError(endpoint_url="u"))
    assert common.failed_before_send(ConnectTimeoutError(endpoint_url="u"))
    for exc in AFTER_SEND_ERRORS:
        assert not common.failed_before_send(exc), exc


def test_pre_send_failures_are_capped(modules, monkeypatch):
    common, _, runner, _ = modules
    _approved_row(common, "appr-r-cap")

    def _boom(session_id, payload, **kw):
        raise ConnectTimeoutError(endpoint_url="https://agentcore.invalid")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    for expected in ("PENDING", "PENDING", "ERROR"):
        assert runner.resume("appr-r-cap")["current"] == expected
        if expected == "PENDING":
            common.transition("appr-r-cap", "PENDING", "APPROVED")
    assert common.get("appr-r-cap")["status"] == "ERROR"


def test_agent_client_makes_exactly_one_attempt(modules, monkeypatch):
    common = modules[0]
    seen = {}

    class FakeBody:
        def read(self):
            return b'{"status": "completed", "text": "ok"}'

    class FakeAgentCore:
        def invoke_agent_runtime(self, **kwargs):
            seen["kwargs"] = kwargs
            return {"response": FakeBody()}

    def fake_client(service, **kwargs):
        seen["service"], seen["config"] = service, kwargs.get("config")
        return FakeAgentCore()

    monkeypatch.setattr(common.boto3, "client", fake_client)
    assert common.invoke_agent("s" * 40, {"prompt": "p"})["status"] == "completed"
    assert seen["service"] == "bedrock-agentcore"
    assert seen["config"].retries["total_max_attempts"] == 1
    assert json.loads(seen["kwargs"]["payload"]) == {"prompt": "p"}


def test_sweeper_rescue_of_stale_resuming_ends_in_error(modules, monkeypatch):
    # Regression: a RESUMING row older than 20 minutes used to go back to
    # PENDING, although its resume may already have run the paused call.
    common, _, runner, _ = modules
    _approved_row(common, "appr-stuck")
    common.transition("appr-stuck", "APPROVED", "RESUMING", resuming_ts=1)
    calls = []
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: calls.append(p) or {})
    out = runner.sweep()
    assert out["rescued"] == 1
    row = common.get("appr-stuck")
    assert row["status"] == "ERROR"
    assert common.public_view(row)["result_text"] == "ResumeOutcomeUnknown"
    assert calls == []


def _expired_pending(common, approval_id):
    common.create_row(approval_id, "s" * 40, "approval")
    common.transition(
        approval_id, "STARTING", "PENDING", interrupts='[{"interruptId": "i1"}]', card="{}"
    )
    common._table.items[approval_id]["expires_ts"] = 1


@pytest.mark.parametrize(
    ("raised", "answer", "status"),
    [
        (ReadTimeoutError(endpoint_url="u"), None, "ERROR"),
        (RuntimeError("boom"), None, "ERROR"),
        (EndpointConnectionError(endpoint_url="u"), None, "PENDING"),
        (None, {"status": "error", "error": "agent fell over"}, "ERROR"),
        (None, {"status": "completed", "text": "refused"}, "EXPIRED"),
    ],
)
def test_sweeper_expiry_applies_the_same_rule(modules, monkeypatch, raised, answer, status):
    common, _, runner, _ = modules
    _expired_pending(common, "appr-exp")

    def _invoke(session_id, payload, **kw):
        if raised is not None:
            raise raised
        return answer

    monkeypatch.setattr(common, "invoke_agent", _invoke)
    runner.sweep()
    assert common.get("appr-exp")["status"] == status


def test_repause_keeps_every_decision(modules, monkeypatch):
    # Regression: a re-pause used to blank decided_by and decided_by_name.
    common, api, runner, _ = modules
    _approved_row(common, "appr-twice")
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: INTERRUPT_PAYLOAD)
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    assert runner.resume("appr-twice")["status"] == "interrupt-again"
    second = {"sub": "sub-9", "username": "second", "cognito:groups": "approvers"}
    assert _decide(api, second, "decline", "appr-twice")["statusCode"] == 202
    assert common.get("appr-twice")["decided_at"]
    assert runner.resume("appr-twice")["status"] == "interrupt-again"
    row = common.get("appr-twice")
    assert row["status"] == "PENDING"
    assert row["decided_by_name"] == "second"
    assert [(d["decision"], d["decided_by_name"]) for d in row["decisions"]] == [
        ("approve", "owner"),
        ("decline", "second"),
    ]
    assert row["decisions"][0]["at"] == "2026-10-20T10:00:00+00:00"
    view = common.public_view(row)
    assert view["decisions"] == [
        {key: entry[key] for key in ("decision", "decided_by_name", "at")}
        for entry in row["decisions"]
    ]
    assert "sub-1" not in json.dumps(view) and "sub-9" not in json.dumps(view)
    # The row keeps "second", but nobody has decided this pause yet.
    assert view["decided_by_name"] == ""


def test_try_decision_records_decided_at(modules, monkeypatch):
    common, api, _, _ = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    common.create_row("appr-try-at", "s" * 40, "try")
    common.transition(
        "appr-try-at", "STARTING", "PENDING", interrupts="[]", card=json.dumps({"layer": "decider"})
    )
    event = _event(
        "POST /try/{token}/decision", {}, {"decision": "decline"}, {"token": "appr-try-at"}
    )
    assert api.handler(event, None)["statusCode"] == 202
    assert common.get("appr-try-at")["decided_at"]


def test_public_view_is_a_whitelist(modules):
    # Regression: public_view returned the raw ERROR text (exception
    # message included) to the anonymous /try lane.
    common = modules[0]
    secret_text = "ClientError: An error occurred (AccessDenied) arn:aws:iam::123456789012:role/x"
    row = {
        "approval_id": "appr-pv",
        "session_id": "lachesis-session-secret-value-0000000000",
        "status": "ERROR",
        "lane": "try",
        "decided_by": "cognito-sub-secret",
        "decided_by_name": "owner",
        "interrupts": json.dumps([{"interruptId": "i1", "reason": "r", "args": {"k": "v"}}]),
        "card": json.dumps(
            {
                "reason": "pause reason",
                "tool": "delete_file",
                "layer": "refusal",
                "args": {"path": "x"},
                "session_id": "lachesis-session-secret-value-0000000000",
                "prior_steps": [{"tool": "write_file", "verdict": "proceed", "args": {"a": 1}}],
            }
        ),
        "result_text": secret_text,
        "decisions": [{"decision": "approve", "decided_by_name": "owner", "at": "t", "sub": "x"}],
        "created_ts": 5,
    }
    view = common.public_view(row)
    assert set(view) == {
        "approval_id", "status", "lane", "card", "decided_by_name",
        "decisions", "result_text", "created_ts",
    }
    assert view["result_text"] == "ClientError"
    dumped = json.dumps(view)
    for leaked in ("cognito-sub-secret", "lachesis-session-secret", "123456789012", "AccessDenied"):
        assert leaked not in dumped, leaked
    assert "args" not in view["card"] and "session_id" not in view["card"]
    assert view["card"]["prior_steps"] == [{"tool": "write_file", "verdict": "proceed"}]
    assert view["decisions"] == [{"decision": "approve", "decided_by_name": "owner", "at": "t"}]
    dotted = "botocore.exceptions.ReadTimeoutError"
    for text, label in (
        ("", ""),
        ("no colon here at all", "Error"),
        ("Bad name!: x", "Error"),
        (dotted + ": x", dotted),
    ):
        assert common.public_view({**row, "result_text": text})["result_text"] == label
    done = common.public_view({**row, "status": "DONE", "result_text": "final: answer"})
    assert done["result_text"] == "final: answer"
    assert common.public_view({**row, "card": "not json"})["card"] == {}
    assert common.public_view({**row, "card": "[1]", "decisions": "x"})["card"] == {}


def test_try_get_returns_the_whitelisted_view(modules):
    common, api, _, _ = modules
    common.create_row("appr-try-get", "s" * 40, "try")
    common.transition("appr-try-get", "STARTING", "ERROR", result_text="ValueError: secret detail")
    out = api.handler(_event("GET /try/{token}", {}, None, {"token": "appr-try-get"}), None)
    body = json.loads(out["body"])
    assert body["result_text"] == "ValueError"
    assert "session_id" not in body and "secret detail" not in out["body"]


def test_console_labels_every_layer_the_pattern_emits():
    import re

    root = Path(__file__).resolve().parents[1] / "src" / "lachesis"
    source = "".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    layers = set(re.findall(r'Verdict\((?:[^()]|\([^()]*\))*?"(\w+)"\s*,?\s*\)', source))
    layers.add("decider")
    assert {"refusal", "unavailable", "secretscan", "tier0"} <= layers
    js = _console("app.js")
    line = next(row for row in js.splitlines() if row.startswith("const LAYER = {"))
    keys = set(re.findall(r"(\w+): \"", line))
    assert layers <= keys, layers - keys


def test_console_try_start_race_keeps_polling():
    # Regression: POST /try returns its token before the async runner
    # writes the row, and the first GET 404 ended the page with "No demo
    # run matches this link".
    js = _console("app.js")
    start = js.index('$("#try-start").addEventListener("click"')
    branch = js[start:js.index("\n});", start)]
    store_at = branch.index('s.setItem("lachesis_try_started"')
    assert store_at < branch.index("location.hash =")
    assert "t: Date.now()" in branch
    view = js[js.index("function viewTry(token)"):start]
    assert 'getItem("lachesis_try_started")' in view
    assert "!denied && mine.token === token && Date.now() - mine.t < 20000" in view
    assert 'renderTry({ status: "STARTING", card: {} }, ctl); ctl.kick(); return;' in view
    assert view.index("ctl.kick()") < view.index("No demo run matches this link")
    # kick() re-arms a stopped poll on a 2 s tick, and 401/403 pass the
    # response, so a denied request is never retried.
    assert "handle = later(tick, 2000)" in js
    assert "on404(out); return;" in js and "on404(); return;" in js
    html = _console("index.html")
    assert "app.js?v=20261020c" in html


def test_console_hides_previous_approver_on_a_fresh_pause():
    js = _console("app.js")
    assert '["Decided by", view.decided_by_name && view.status !== "PENDING" ?' in js


def _decisions(row):
    return [(d["decision"], d["decided_by_name"]) for d in row.get("decisions", [])]


@pytest.mark.parametrize(
    ("lane", "answer", "final"),
    [
        ("try", None, "EXPIRED"),
        ("approval", {"status": "completed", "text": "refused"}, "EXPIRED"),
        ("approval", {"status": "error", "error": "agent fell over"}, "ERROR"),
    ],
)
def test_expiry_after_a_repause_credits_nobody(modules, monkeypatch, lane, answer, final):
    # Regression: a re-pause keeps the earlier decider in the row, and an
    # expiry of the new pause showed that person as having decided it
    # ("Nobody decided before the deadline. Decided on this page.").
    common, api, runner, _ = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    aid = "appr-exp-" + lane + "-" + final
    common.create_row(aid, "s" * 40, lane)
    common.transition(
        aid,
        "STARTING",
        "PENDING",
        interrupts='[{"interruptId": "i1"}]',
        card=json.dumps({"layer": "decider", "tool": "delete_file"}),
    )
    if lane == "try":
        event = _event("POST /try/{token}/decision", {}, {"decision": "approve"}, {"token": aid})
        assert api.handler(event, None)["statusCode"] == 202
        first = "try-visitor"
    else:
        assert _decide(api, APPROVER, "approve", aid)["statusCode"] == 202
        first = "owner"
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: INTERRUPT_PAYLOAD)
    assert runner.resume(aid)["status"] == "interrupt-again"
    assert common.public_view(common.get(aid))["decided_by_name"] == ""
    common._table.items[aid]["expires_ts"] = 1
    calls = []
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: calls.append(p) or answer)
    runner.sweep()
    row = common.get(aid)
    assert row["status"] == final
    assert len(calls) == (0 if lane == "try" else 1)
    assert row["decided_by"] == "" and row["decided_by_name"] == "" and row["decided_at"] == ""
    assert common.public_view(row)["decided_by_name"] == ""
    assert _decisions(row) == [("approve", first)]
    assert common.public_view(row)["card"]["tool"] == "run_tests"


def test_public_view_names_the_decider_only_once_decided(modules):
    common = modules[0]
    row = {"approval_id": "a", "status": "PENDING", "decided_by_name": "owner"}
    for status, shown in (
        ("PENDING", ""),
        ("EXPIRED", ""),
        ("APPROVED", "owner"),
        ("RESUMING", "owner"),
        ("DONE", "owner"),
        ("ERROR", "owner"),
    ):
        assert common.public_view({**row, "status": status})["decided_by_name"] == shown


def test_pre_send_failure_keeps_the_unsent_decision(modules, monkeypatch):
    # Regression: a resume that failed before send returned the row to
    # PENDING with the decider still set and no history entry, so the
    # next decision overwrote the first and it appeared nowhere.
    common, api, runner, _ = modules
    monkeypatch.setattr(api, "_invoke_runner", lambda payload, asynchronous: {})
    _approved_row(common, "appr-unsent")

    def _boom(session_id, payload, **kw):
        raise ConnectTimeoutError(endpoint_url="https://agentcore.invalid")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    assert runner.resume("appr-unsent")["current"] == "PENDING"
    row = common.get("appr-unsent")
    assert row["decided_by"] == "" and row["decided_by_name"] == "" and row["decided_at"] == ""
    assert row["decisions"] == [
        {
            "decision": "approve_not_sent",
            "decided_by_name": "owner",
            "at": "2026-10-20T10:00:00+00:00",
        }
    ]
    second = {"sub": "sub-9", "username": "second", "cognito:groups": "approvers"}
    assert _decide(api, second, "decline", "appr-unsent")["statusCode"] == 202
    monkeypatch.setattr(
        common, "invoke_agent", lambda s, p, **kw: {"status": "completed", "text": "t"}
    )
    assert runner.resume("appr-unsent")["status"] == "REFUSED_DONE"
    view = common.public_view(common.get("appr-unsent"))
    assert view["decided_by_name"] == "second"
    assert _decisions(view) == [("approve_not_sent", "owner")]


def test_sweeper_pre_send_failure_adds_no_person_decision(modules, monkeypatch):
    common, _, runner, _ = modules
    _expired_pending(common, "appr-exp-unsent")

    def _boom(session_id, payload, **kw):
        raise EndpointConnectionError(endpoint_url="u")

    monkeypatch.setattr(common, "invoke_agent", _boom)
    runner.sweep()
    row = common.get("appr-exp-unsent")
    assert row["status"] == "PENDING" and row["sweep_attempts"] == 1
    assert "decisions" not in row


def _try_card(reason, tool):
    return json.dumps({"reason": reason, "tool": tool, "layer": "decider"})


def _split_reason(reason):
    """The console's parseReason split, in Python."""
    rest = reason[reason.index("\narguments:\n") + 12 :]
    cut = rest.rindex("\n\n")
    return json.loads(rest[:cut]), rest[cut + 2 :]


def test_try_lane_reason_keeps_only_whitelisted_arguments(modules):
    import re

    from lachesis.handler import _confirm_prompt
    from lachesis.policy import decide

    common = modules[0]
    verdict = decide("destructive_write", 0.85, 0.9)

    def view(tool, args, lane="try"):
        reason = _confirm_prompt(tool, args, verdict, 0.7)
        row = {
            "approval_id": "a",
            "status": "PENDING",
            "lane": lane,
            "card": _try_card(reason, tool),
        }
        return reason, common.public_view(row)["card"]["reason"]

    long_text = "Workspace check started. " * 40
    original, shown = view(
        "write_file",
        {
            "path": "cleanup_notes.txt",
            "content": long_text,
            "mode": "extra-arg-value",
            "nested": {"k": "v"},
        },
    )
    args, message = _split_reason(shown)
    assert args == {"path": "cleanup_notes.txt", "content": long_text[:200] + "..."}
    assert "extra-arg-value" not in shown and "nested" not in shown
    assert message == verdict.message
    assert shown.split("\narguments:\n")[0] == original.split("\narguments:\n")[0]
    pattern = r"Decider: (\S+) at ([0-9.]+), threshold ([0-9.]+); grounded: (yes|no)"
    assert re.search(pattern, shown)
    _, shown = view("delete_file", {"path": "refunds_prod_config.py", "force": "yes-extra"})
    assert _split_reason(shown)[0] == {"path": "refunds_prod_config.py"}
    _, shown = view("run_tests", {"selector": "extra"})
    assert _split_reason(shown)[0] == {}
    _, shown = view("exfiltrate", {"path": "p", "url": "https://example.invalid"})
    assert _split_reason(shown)[0] == {} and "example.invalid" not in shown
    # The approver lane keeps the full Confirm prompt.
    original, shown = view("delete_file", {"path": "p", "force": "x"}, lane="approval")
    assert shown == original
    # Arguments that are not a JSON object never pass through.
    capped = 't\ntool: read_file\narguments:\n{"path": "x"... [9 more]\n\nm'
    row = {
        "approval_id": "a",
        "status": "PENDING",
        "lane": "try",
        "card": _try_card(capped, "read_file"),
    }
    assert common.public_view(row)["card"]["reason"].endswith("arguments:\n{}\n\nm")
    plain = {**row, "card": _try_card("plain reason", "write_file")}
    assert common.public_view(plain)["card"]["reason"] == "plain reason"


def test_try_counter_rows_carry_a_ttl(modules):
    import time as _time

    _, api, _, table = modules
    event = {"requestContext": {"http": {"sourceIp": "203.0.113.11"}}}
    assert api._try_counter_ok(event) is True
    first = {key: row["ttl"] for key, row in table.items.items() if key.startswith("try-")}
    assert len(first) == 2
    for value in first.values():
        assert 0 < value - _time.time() <= api.COUNTER_TTL_SECONDS
    assert api._try_counter_ok(event) is True
    assert {key: table.items[key]["ttl"] for key in first} == first


class _Context:
    def __init__(self, remaining_ms):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self.remaining_ms


def test_agent_timeouts_are_explicit_and_follow_the_lambda_budget(modules, monkeypatch):
    common, _, runner, _ = modules
    config = common.AGENT_CLIENT_CONFIG
    assert config.read_timeout == common.AGENT_READ_TIMEOUT == 840
    assert config.connect_timeout == common.AGENT_CONNECT_TIMEOUT == 10
    assert common.agent_client_config(100).read_timeout == 100
    assert common.agent_client_config(100).retries["total_max_attempts"] == 1
    assert runner._read_timeout(None) == 840
    assert runner._read_timeout(_Context(300_000)) == 280
    assert runner._read_timeout(_Context(2_000_000)) == 840
    assert runner._read_timeout(_Context(10_000)) == 5
    seen = []
    monkeypatch.setattr(
        common,
        "invoke_agent",
        lambda s, p, **kw: seen.append(kw["read_timeout"]) or {"status": "completed", "text": ""},
    )
    _approved_row(common, "appr-budget")
    runner.handler({"action": "resume", "approval_id": "appr-budget"}, _Context(120_000))
    runner.handler({"action": "start", "prompt": "p", "lane": "try"}, _Context(900_000))
    assert seen == [100, 840]


def test_agent_client_uses_the_given_read_timeout(modules, monkeypatch):
    common = modules[0]
    seen = {}

    class FakeBody:
        def read(self):
            return b'{"status": "completed", "text": "ok"}'

    class FakeAgentCore:
        def invoke_agent_runtime(self, **kwargs):
            return {"response": FakeBody()}

    def fake_client(service, **kwargs):
        seen["config"] = kwargs.get("config")
        return FakeAgentCore()

    monkeypatch.setattr(common.boto3, "client", fake_client)
    common.invoke_agent("s" * 40, {"prompt": "p"}, read_timeout=42)
    assert seen["config"].read_timeout == 42
    assert seen["config"].retries["total_max_attempts"] == 1


def test_sweeper_defers_expiry_resumes_without_lambda_time(modules, monkeypatch):
    common, _, runner, _ = modules
    calls = []
    monkeypatch.setattr(common, "invoke_agent", lambda s, p, **kw: calls.append(p) or {})
    for aid, lane in (("appr-late-try", "try"), ("appr-late-appr", "approval")):
        common.create_row(aid, "s" * 40, lane)
        common.transition(aid, "STARTING", "PENDING", interrupts="[]", card="{}")
        common._table.items[aid]["expires_ts"] = 1
    spec = importlib.util.spec_from_file_location("sweeper", LAMBDA_DIR / "sweeper.py")
    sweeper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweeper)
    monkeypatch.setattr(sweeper, "runner", runner)
    out = sweeper.handler({}, _Context(30_000))
    assert out["deferred"] == 1 and out["closed_try"] == 1
    assert common.get("appr-late-appr")["status"] == "PENDING"
    assert common.get("appr-late-try")["status"] == "EXPIRED"
    assert calls == []


@pytest.mark.parametrize(
    ("errors", "final", "calls_made", "sleeps"),
    [
        ([_client_error(429, "ThrottlingException")] * 2, "completed", 3, [1.0, 2.0]),
        ([_client_error(402, "ServiceQuotaExceededException")], "completed", 2, [1.0]),
        ([ConnectTimeoutError(endpoint_url="u")], "completed", 2, [1.0]),
        ([_client_error(429, "ThrottlingException")] * 3, "error", 3, [1.0, 2.0]),
        ([ReadTimeoutError(endpoint_url="u")], "error", 1, []),
        ([_client_error(500, "InternalServerException")], "error", 1, []),
    ],
    ids=[
        "throttle-twice",
        "quota-once",
        "pre-send-once",
        "throttle-thrice",
        "read-timeout",
        "server-error",
    ],
)
def test_start_retries_only_refusals_that_never_ran(
    modules, monkeypatch, errors, final, calls_made, sleeps
):
    common, _, runner, _ = modules
    pending = list(errors)
    calls, slept = [], []

    def _invoke(session_id, payload, **kw):
        calls.append(payload)
        if pending:
            raise pending.pop(0)
        return {"status": "completed", "text": "ok"}

    monkeypatch.setattr(common, "invoke_agent", _invoke)
    monkeypatch.setattr(runner, "_sleep", slept.append)
    out = runner.start("p", "try", "appr-start-retry")
    assert out["status"] == final
    assert len(calls) == calls_made and slept == sleeps
    expected_row = "DONE" if final == "completed" else "ERROR"
    assert common.get("appr-start-retry")["status"] == expected_row

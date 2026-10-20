"""B6, the lachesis-run Step Functions workflow, offline.

Nothing here talks to AWS. DynamoDB is test_approvals' in-memory fake
(conditions evaluated, transactions all or nothing); Step Functions is a
fake client plus a small interpreter of the state machine definition
(infra/stacks/run_workflow.py) that drives the real task Lambda
(approvals/lambda/workflow.py) through Task, Choice, Catch, ResultSelector
and ResultPath, and holds a waitForTaskToken task until the API's
SendTaskSuccess (or a simulated timeout) releases it. The agent is a
scripted stub of common.invoke_agent.

Both flag states are covered: with USE_STEP_FUNCTIONS unset or "false"
the API never builds a Step Functions client and the runner path is the
one test_approvals pins; with it "true" a start is a StartExecution and a
decision a SendTaskSuccess, and the workflow writes every status.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import itertools
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from test_approvals import (  # noqa: F401  (modules is a fixture)
    APPROVER,
    LAMBDA_DIR,
    PAUSE_PAYLOAD,
    _event,
    _Lambda,
    _post_try,
    modules,
)

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "infra" / "stacks" / "approvals.py"
ARN = "arn:aws:states:eu-west-1:123456789012:stateMachine:lachesis-run"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


asl = _load("run_workflow_asl", ROOT / "infra" / "stacks" / "run_workflow.py")


# --- fakes ------------------------------------------------------------


def _sfn_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeSfn:
    """The Step Functions API the API and the task Lambda call. A start
    is recorded; a token answer goes to the interpreter, which owns the
    one open wait."""

    def __init__(self):
        self.calls = []
        self.configs = []
        self.raise_on = {}  # operation -> error code to raise once
        self.sim = None

    def client(self, config):
        self.configs.append(config)
        return self

    def _maybe_raise(self, operation):
        code = self.raise_on.pop(operation, None)
        if code:
            raise _sfn_error(code, operation)

    def start_execution(self, **kwargs):
        self.calls.append(("start_execution", kwargs))
        self._maybe_raise("StartExecution")
        return {"executionArn": ARN.replace("stateMachine", "execution") + ":" + kwargs["name"]}

    def send_task_success(self, taskToken, output):  # noqa: N803
        self.calls.append(("send_task_success", {"taskToken": taskToken, "output": output}))
        self._maybe_raise("SendTaskSuccess")
        if self.sim is None or not self.sim.answer(taskToken, json.loads(output)):
            raise _sfn_error("TaskTimedOut", "SendTaskSuccess")
        return {}

    def send_task_failure(self, taskToken, error, cause):  # noqa: N803
        self.calls.append(("send_task_failure", {"taskToken": taskToken, "error": error}))
        if self.sim is None or not self.sim.fail_task(taskToken, error, cause):
            raise _sfn_error("TaskTimedOut", "SendTaskFailure")
        return {}

    def ops(self):
        return [name for name, _ in self.calls]


def _path(path, data, token=""):
    if path == "$$.Task.Token":
        return token
    assert path == "$" or path.startswith("$."), path
    value = data
    for part in [p for p in path[2:].split(".") if p]:
        value = value[part]
    return value


def _template(template, data, token=""):
    out = {}
    for key, value in template.items():
        if key.endswith(".$"):
            out[key[:-2]] = _path(value, data, token)
        elif isinstance(value, dict):
            out[key] = _template(value, data, token)
        else:
            out[key] = value
    return out


def _with_result(data, result_path, value):
    if result_path is None:
        return data
    data = copy.deepcopy(data)
    if result_path == "$":
        return copy.deepcopy(value)
    parts = result_path[2:].split(".")
    target = data
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = copy.deepcopy(value)
    return data


class Sim:
    """One execution of the definition, run state by state. Retries are
    not simulated (they name only Lambda service errors, which the task
    function never raises); retried invocations are tested directly."""

    def __init__(self, workflow, sfn, definition=None):
        self.definition = definition or asl.definition()
        self.workflow = workflow
        self.sfn = sfn
        sfn.sim = self
        self.data = None
        self.state = None
        self.ended = None
        self.trail = []
        self.waiting = None  # (state name, token)
        self.bounds = []  # (heartbeat_s, timeout_s) per wait
        self.answered = None
        self.failed = None
        self.payloads = []
        self._ids = itertools.count(1)

    def start(self, approval_id):
        self.data = {"approval_id": approval_id}
        self.state = self.definition["StartAt"]
        self._run()
        return self

    def _catch(self, spec, error, cause):
        for catcher in spec.get("Catch", []):
            if error in catcher["ErrorEquals"] or "States.ALL" in catcher["ErrorEquals"]:
                self.data = _with_result(
                    self.data, catcher["ResultPath"], {"Error": error, "Cause": cause}
                )
                self.state = catcher["Next"]
                return
        raise AssertionError(f"uncaught {error} in {self.state}")

    def _invoke(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        return self.workflow.handler(payload, _Lambda(f"req-{next(self._ids)}"))

    def _run(self):
        while True:
            name = self.state
            spec = self.definition["States"][name]
            self.trail.append(name)
            kind = spec["Type"]
            if kind in {"Succeed", "Fail"}:
                self.ended = name
                return
            if kind == "Choice":
                value = None
                self.state = spec["Default"]
                for rule in spec["Choices"]:
                    value = _path(rule["Variable"], self.data)
                    if value == rule["StringEquals"]:
                        self.state = rule["Next"]
                        break
                continue
            assert kind == "Task"
            if spec["Resource"].endswith(".waitForTaskToken"):
                token = f"tok-{next(self._ids)}-" + "x" * 40
                self.bounds.append(
                    (
                        _path(spec["HeartbeatSecondsPath"], self.data),
                        _path(spec["TimeoutSecondsPath"], self.data),
                    )
                )
                self.waiting = (name, token)
                self.failed = None
                payload = _template(spec["Parameters"]["Payload"], self.data, token)
                try:
                    self._invoke(payload)
                except Exception as exc:
                    self.waiting = None
                    self._catch(spec, type(exc).__name__, str(exc))
                    continue
                if self.failed is not None:
                    error, cause = self.failed
                    self._catch(spec, error, cause)
                    continue
                return  # waits for answer() or time_out()
            payload = _template(spec["Parameters"]["Payload"], self.data)
            try:
                raw = self._invoke(payload)
            except Exception as exc:
                self._catch(spec, type(exc).__name__, str(exc))
                continue
            result = {"Payload": raw}
            if "ResultSelector" in spec:
                result = _template(spec["ResultSelector"], result)
            self.data = _with_result(self.data, spec.get("ResultPath", "$"), result)
            self.state = spec["Next"]

    def answer(self, token, output):
        """SendTaskSuccess: False when no wait holds this token."""
        if not self.waiting or self.waiting[1] != token:
            return False
        name, _ = self.waiting
        self.waiting = None
        self.answered = (name, output)
        return True

    def fail_task(self, token, error, cause):
        if not self.waiting or self.waiting[1] != token:
            return False
        self.waiting = None
        self.failed = (error, cause)
        return True

    def drain(self):
        """Continue after an answered wait (Step Functions runs on its own;
        here the test decides when)."""
        name, output = self.answered
        self.answered = None
        spec = self.definition["States"][name]
        self.data = _with_result(self.data, spec["ResultPath"], output)
        self.state = spec["Next"]
        self._run()
        return self

    def time_out(self, error="States.HeartbeatTimeout"):
        name, _ = self.waiting
        self.waiting = None
        self._catch(self.definition["States"][name], error, "the wait ended")
        self._run()
        return self


class Agent:
    """A scripted common.invoke_agent: one answer (or exception) per call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, session_id, payload, **kwargs):
        self.calls.append(copy.deepcopy(payload))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return copy.deepcopy(answer)


COMPLETED = {"status": "completed", "text": "all done"}


@pytest.fixture()
def wf(modules, monkeypatch):  # noqa: F811
    common, api, runner, table = modules
    loaded = {}
    for name in ("orchestrator", "workflow"):
        spec = importlib.util.spec_from_file_location(name, LAMBDA_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[name] = module
    orchestrator, workflow = loaded["orchestrator"], loaded["workflow"]
    monkeypatch.setattr(api, "orchestrator", orchestrator)
    assert workflow.runner is runner and workflow.common is common
    assert orchestrator.common is common
    sfn = FakeSfn()
    monkeypatch.setattr(orchestrator, "_sfn", sfn.client)
    monkeypatch.setattr(workflow, "_sfn", lambda: sfn.client(None))
    monkeypatch.setattr(workflow, "_sleep", lambda seconds: None)
    monkeypatch.setattr(runner, "_sleep", lambda seconds: None)
    runner_calls = []
    monkeypatch.setattr(
        api,
        "_invoke_runner",
        lambda payload, asynchronous: runner_calls.append(payload) or {},
    )
    monkeypatch.setenv("STATE_MACHINE_ARN", ARN)
    monkeypatch.delenv("USE_STEP_FUNCTIONS", raising=False)
    return SimpleNamespace(
        common=common,
        api=api,
        runner=runner,
        table=table,
        orchestrator=orchestrator,
        workflow=workflow,
        sfn=sfn,
        runner_calls=runner_calls,
        domain=sys.modules["approval.domain"],
    )


def _flag_on(monkeypatch):
    monkeypatch.setenv("USE_STEP_FUNCTIONS", "true")


def _try_decide(w, token, decision="approve"):
    event = _event("POST /try/{token}/decision", {}, {"decision": decision}, {"token": token})
    return w.api.handler(event, None)


def _approver_decide(w, approval_id, decision):
    event = _event(
        "POST /approvals/{id}/decision", APPROVER, {"decision": decision}, {"id": approval_id}
    )
    return w.api.handler(event, None)


def _start_try(w):
    out = _post_try(w.api, "198.51.100.9")
    assert out["statusCode"] == 202, out
    return json.loads(out["body"])["token"]


def _events(w, approval_id):
    return [row["event_type"] for row in w.table.dynamo.events_of(approval_id)]


# --- the definition ---------------------------------------------------


def _states():
    return asl.definition()["States"]


def _nexts(spec):
    targets = [spec.get("Next"), spec.get("Default")]
    targets += [rule["Next"] for rule in spec.get("Choices", [])]
    targets += [catcher["Next"] for catcher in spec.get("Catch", [])]
    return [target for target in targets if target]


def test_definition_is_json_and_every_state_is_linked_and_reachable():
    definition = asl.definition()
    assert json.loads(json.dumps(definition)) == definition
    states = definition["States"]
    assert definition["StartAt"] == "AgentTurn"
    for name, spec in states.items():
        for target in _nexts(spec):
            assert target in states, (name, target)
    seen, todo = set(), [definition["StartAt"]]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo.extend(_nexts(states[name]))
    assert seen == set(states)
    terminal = {name: spec["Type"] for name, spec in states.items() if spec["Type"] in {
        "Succeed", "Fail"}}
    assert terminal == {"Completed": "Succeed", "Refused": "Succeed", "Failed": "Fail"}
    for name in terminal:
        assert "Next" not in states[name] and "End" not in states[name]


def test_every_task_invokes_the_task_function_and_retries_only_lambda_service_errors():
    states = _states()
    tasks = {name: spec for name, spec in states.items() if spec["Type"] == "Task"}
    assert set(tasks) == {"AgentTurn", "WaitForDecision", "ApplyDecision", "Expire",
                          "RecordFailure"}
    forbidden = {"States.ALL", "States.TaskFailed", "Lambda.ClientExecutionTimeoutException",
                 "States.Timeout"}
    for name, spec in tasks.items():
        assert spec["Parameters"]["FunctionName"] == "${TaskFunctionArn}", name
        assert spec["Parameters"]["Payload"]["approval_id.$"] == "$.approval_id"
        assert spec["Parameters"]["Payload"]["action"] in asl_actions(), name
        [retry] = spec["Retry"]
        assert retry["ErrorEquals"] == [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException",
        ], name
        assert not forbidden & set(retry["ErrorEquals"])
        assert retry["MaxAttempts"] <= 3
        # Every caught failure is recorded, except RecordFailure's own.
        last = spec["Catch"][-1]
        assert last["ErrorEquals"] == ["States.ALL"]
        assert last["Next"] == ("Failed" if name == "RecordFailure" else "RecordFailure")
    assert states["AgentTurn"]["Resource"] == "arn:aws:states:::lambda:invoke"
    # Above the 900 s Lambda limit, so the function's own timeout is caught.
    assert states["AgentTurn"]["TimeoutSeconds"] > 900
    assert states["Expire"]["TimeoutSeconds"] > 900


def asl_actions():
    return {"turn", "await", "apply", "expire", "fail"}


def test_the_wait_is_a_task_token_with_bounds_from_the_row():
    wait = _states()["WaitForDecision"]
    assert wait["Resource"] == "arn:aws:states:::lambda:invoke.waitForTaskToken"
    assert wait["Parameters"]["Payload"]["task_token.$"] == "$$.Task.Token"
    assert wait["Parameters"]["Payload"]["action"] == "await"
    assert wait["HeartbeatSecondsPath"] == "$.turn.heartbeat_s"
    assert wait["TimeoutSecondsPath"] == "$.turn.timeout_s"
    assert "TimeoutSeconds" not in wait and "HeartbeatSeconds" not in wait
    assert "ResultSelector" not in wait  # SendTaskSuccess output, unwrapped
    first, second = wait["Catch"]
    assert set(first["ErrorEquals"]) == {"States.Timeout", "States.HeartbeatTimeout"}
    assert first["Next"] == "Expire"
    assert second == {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error",
                      "Next": "RecordFailure"}
    assert wait["Next"] == "ApplyDecision"


def test_choices_route_outcomes_the_task_function_returns(wf):
    states = _states()
    assert states["AgentTurn"]["Next"] == "TurnOutcome"
    assert states["ApplyDecision"]["Next"] == "DecisionOutcome"
    assert states["Expire"]["Next"] == "ExpiryOutcome"
    routes = {
        name: {rule["StringEquals"]: rule["Next"] for rule in spec["Choices"]}
        for name, spec in states.items()
        if spec["Type"] == "Choice"
    }
    assert routes["TurnOutcome"] == {
        "interrupt": "WaitForDecision",
        "completed": "Completed",
        "refused": "Refused",
        "expired": "Refused",
    }
    assert routes["DecisionOutcome"] == {"resume": "AgentTurn", "expired": "Expire"}
    assert routes["ExpiryOutcome"] == {
        "expired": "Refused",
        "refused": "Refused",
        "completed": "Completed",
    }
    for name, spec in states.items():
        if spec["Type"] == "Choice":
            assert spec["Default"] == "Failed", name
    outcomes = {value for route in routes.values() for value in route}
    assert outcomes <= set(wf.workflow.OUTCOMES)
    assert asl_actions() == set(wf.workflow.ACTIONS)


def test_state_carries_ids_and_small_results_only():
    states = _states()
    payloads = [spec["Parameters"]["Payload"] for spec in states.values() if spec["Type"] ==
                "Task"]
    keys = {key for payload in payloads for key in payload}
    assert keys == {"action", "approval_id.$", "task_token.$", "decision.$", "error.$"}
    # ApplyDecision writes its outcome over the decision, so the
    # approver's identity leaves the state at once.
    assert states["ApplyDecision"]["ResultPath"] == "$.decision"
    assert states["ApplyDecision"]["ResultSelector"] == {"outcome.$": "$.Payload.outcome"}
    assert states["RecordFailure"]["Parameters"]["Payload"]["error.$"] == "$.error.Error"
    assert states["RecordFailure"]["ResultPath"] is None
    assert len(json.dumps(asl.definition())) < 16_000


# --- flag off: the runner path, untouched -----------------------------


def test_flag_off_never_builds_a_step_functions_client(wf, monkeypatch):
    for value in (None, "false", "False", "yes", ""):
        if value is None:
            monkeypatch.delenv("USE_STEP_FUNCTIONS", raising=False)
        else:
            monkeypatch.setenv("USE_STEP_FUNCTIONS", value)
        assert wf.orchestrator.enabled() is False
    monkeypatch.setattr(wf.orchestrator, "_sfn", lambda config: pytest.fail("sfn client built"))
    token = _start_try(wf)
    row = wf.table.items[token]
    assert "orchestrator" not in row and "prompt" not in row
    assert wf.runner_calls == [
        {"action": "start", "prompt": wf.api.CANNED_PROMPT, "approval_id": token}
    ]
    ops = [next(iter(entry)) for entry in wf.table.dynamo.transactions[0]]
    assert ops == ["Update", "Update", "Put", "Put"]
    # The runner pauses it, the decision is recorded by the API and
    # handed to the runner, as before B6.
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD))
    wf.runner.start(wf.api.CANNED_PROMPT, token, context=_Lambda("req-1"))
    assert wf.table.items[token]["status"] == "PENDING"
    assert _try_decide(wf, token)["statusCode"] == 202
    assert wf.table.items[token]["status"] == "APPROVED"
    assert wf.runner_calls[-1] == {"action": "resume", "approval_id": token}
    assert wf.sfn.calls == []


def test_flag_off_approver_start_keeps_the_runner_path(wf, monkeypatch):
    monkeypatch.setattr(wf.orchestrator, "_sfn", lambda config: pytest.fail("sfn client built"))
    out = wf.api.handler(_event("POST /approvals/start", APPROVER, {"prompt": "do it"}), None)
    assert out["statusCode"] == 202
    approval_id = json.loads(out["body"])["approval_id"]
    assert wf.runner_calls == [{"action": "start", "prompt": "do it", "approval_id": approval_id}]
    assert "orchestrator" not in wf.table.items[approval_id]


# --- flag on: starts --------------------------------------------------


def test_flag_on_try_start_is_one_start_execution_named_by_the_approval_id(wf, monkeypatch):
    _flag_on(monkeypatch)
    token = _start_try(wf)
    assert wf.runner_calls == []
    [(op, call)] = wf.sfn.calls
    assert op == "start_execution"
    assert call == {"stateMachineArn": ARN, "name": token, "input": json.dumps(
        {"approval_id": token})}
    row = wf.table.items[token]
    assert row["status"] == "STARTING" and row["orchestrator"] == "sfn"
    assert row["prompt"] == wf.api.CANNED_PROMPT
    # Same counters, row and event, in one transaction, as with the flag off.
    ops = [next(iter(entry)) for entry in wf.table.dynamo.transactions[0]]
    assert ops == ["Update", "Update", "Put", "Put"]
    assert len(wf.table.dynamo.transactions) == 1
    # The start client retries (StartExecution is idempotent by name and
    # input); the decision client sends once.
    assert wf.sfn.configs[0] is wf.orchestrator.START_CONFIG
    view = json.loads(
        wf.api.handler(_event("GET /try/{token}", {}, None, {"token": token}), None)["body"]
    )
    assert "prompt" not in json.dumps(view) and "orchestrator" not in view


def test_flag_on_approver_start_stores_the_prompt_and_caps_it(wf, monkeypatch):
    _flag_on(monkeypatch)
    out = wf.api.handler(_event("POST /approvals/start", APPROVER, {"prompt": "do it"}), None)
    assert out["statusCode"] == 202
    approval_id = json.loads(out["body"])["approval_id"]
    assert wf.table.items[approval_id]["prompt"] == "do it"
    assert wf.sfn.calls[0][1]["name"] == approval_id
    too_long = "x" * (wf.api.WORKFLOW_PROMPT_MAX_CHARS + 1)
    refused = wf.api.handler(_event("POST /approvals/start", APPROVER, {"prompt": too_long}),
                             None)
    assert refused["statusCode"] == 400
    assert len(wf.sfn.calls) == 1 and wf.runner_calls == []


def test_a_failed_start_execution_ends_the_run_and_answers_502(wf, monkeypatch, capsys):
    _flag_on(monkeypatch)
    wf.sfn.raise_on["StartExecution"] = "ExecutionLimitExceeded"
    out = _post_try(wf.api, "198.51.100.9")
    assert out["statusCode"] == 502
    [row] = [r for r in wf.table.items.values() if r.get("lane") == "try"]
    assert row["status"] == "ERROR"
    assert '"workflow_start_failed": "ClientError"' in capsys.readouterr().out
    # An execution that already exists is the start we asked for.
    wf.sfn.raise_on["StartExecution"] = "ExecutionAlreadyExists"
    assert _post_try(wf.api, "198.51.100.10")["statusCode"] == 202


# --- flag on: a whole run ---------------------------------------------


def test_try_run_pauses_with_the_token_in_the_same_write(wf, monkeypatch):
    _flag_on(monkeypatch)
    agent = Agent(PAUSE_PAYLOAD, COMPLETED)
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    token = _start_try(wf)
    before = len(wf.table.dynamo.transactions)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    assert sim.trail == ["AgentTurn", "TurnOutcome", "WaitForDecision"]
    assert agent.calls[0] == {"prompt": wf.api.CANNED_PROMPT, "session_id": wf.table.items[
        token]["session_id"]}
    row = wf.table.items[token]
    assert row["status"] == "PENDING" and row["task_token"] == sim.waiting[1]
    assert row["staged_pause"] == ""
    # claim, stage, then ONE write makes the pause decidable and stores
    # the token: the status, the token and RunPaused together.
    claim, stage, opened = wf.table.dynamo.transactions[before:]
    assert [next(iter(item)) for item in stage] == ["Update"]  # no event yet
    set_names = re.findall(r"(#a\d+) = ", stage[0]["Update"]["UpdateExpression"])
    staged = {stage[0]["Update"]["ExpressionAttributeNames"][name] for name in set_names}
    assert staged == {"staged_pause", "version"}
    assert stage[0]["Update"]["ExpressionAttributeValues"][":expected_status"] == "STARTING"
    update = opened[0]["Update"]
    assigned = {
        update["ExpressionAttributeNames"][name]
        for name in re.findall(r"(#a\d+) = ", update["UpdateExpression"])
    }
    assert {"status", "task_token", "card", "interrupts", "pause_seq"} <= assigned
    assert [next(iter(item)) for item in opened] == ["Update", "Put"]
    assert opened[1]["Put"]["Item"]["event_type"] == "RunPaused"
    # The wait's bounds are the row's expiry: heartbeat at the expiry,
    # the task timeout a few seconds later.
    [(heartbeat, timeout)] = sim.bounds
    remaining = int(row["expires_ts"]) - int(time.time())
    assert abs(heartbeat - remaining) <= 2 and timeout == heartbeat + wf.workflow.WAIT_GRACE_S
    card = json.loads(row["card"])
    assert card["expires_at"] == wf.domain.iso(int(row["expires_ts"]))
    # The token is never public: not in GET, the ledger, or any event.
    view = wf.api.handler(_event("GET /try/{token}", {}, None, {"token": token}), None)
    ledger = wf.api.handler(_event("GET /try/{token}/events", {}, None, {"token": token}), None)
    for text in (view["body"], ledger["body"], json.dumps(wf.table.dynamo.events_of(token),
                                                          default=str)):
        assert sim.waiting[1] not in text and "task_token" not in text
        assert "staged_pause" not in text and "resume_claim" not in text


def test_try_decision_is_a_send_task_success_and_the_workflow_records_it(wf, monkeypatch):
    _flag_on(monkeypatch)
    agent = Agent(PAUSE_PAYLOAD, COMPLETED)
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    task_token = sim.waiting[1]
    row_before = copy.deepcopy(wf.table.items[token])
    out = _try_decide(wf, token, "approve")
    assert out["statusCode"] == 202
    assert json.loads(out["body"]) == {"token": token, "status": "PENDING"}
    # The API wrote nothing: the row is as it was, and no runner call.
    assert wf.table.items[token] == row_before and wf.runner_calls == []
    op, call = wf.sfn.calls[-1]
    assert op == "send_task_success" and call["taskToken"] == task_token
    output = json.loads(call["output"])
    assert output["decision"] == "approve" and output["decided_by_name"] == "try-visitor"
    assert wf.sfn.configs[-1] is wf.orchestrator.DECISION_CONFIG
    sim.drain()
    assert sim.trail[-5:] == [
        "ApplyDecision", "DecisionOutcome", "AgentTurn", "TurnOutcome", "Completed",
    ]
    assert sim.ended == "Completed"
    assert agent.calls[1]["interrupt_responses"] == [
        {"interruptId": "int-secret-7", "response": "y"}
    ]
    row = wf.table.items[token]
    assert row["status"] == "DONE" and row["result_text"] == "all done"
    assert row["task_token"] == ""
    assert _events(wf, token) == [
        "RunRequested", "RunClaimed", "RunPaused", "DecisionRecorded", "RunResuming",
        "RunCompleted",
    ]
    # The decision left the state: ApplyDecision's outcome replaced it.
    assert sim.data["decision"] == {"outcome": "resume"}


def test_approval_decline_ends_refused_and_keeps_the_sub_off_events(wf, monkeypatch):
    _flag_on(monkeypatch)
    agent = Agent(PAUSE_PAYLOAD, COMPLETED)
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    out = wf.api.handler(_event("POST /approvals/start", APPROVER, {"prompt": "go"}), None)
    approval_id = json.loads(out["body"])["approval_id"]
    sim = Sim(wf.workflow, wf.sfn).start(approval_id)
    queue = json.loads(wf.api.handler(_event("GET /approvals", APPROVER), None)["body"])
    assert [v["approval_id"] for v in queue["approvals"]] == [approval_id]
    assert _approver_decide(wf, approval_id, "decline")["statusCode"] == 202
    sim.drain()
    assert sim.ended == "Refused"
    row = wf.table.items[approval_id]
    assert row["status"] == "REFUSED_DONE"
    assert row["decided_by"] == "sub-1" and row["decided_by_name"] == "owner"
    assert agent.calls[1]["interrupt_responses"][0]["response"] == "no"
    events = json.dumps(wf.table.dynamo.events_of(approval_id), default=str)
    assert "sub-1" not in events


@pytest.mark.parametrize("code", ["TaskTimedOut", "InvalidToken", "TaskDoesNotExist"])
def test_a_wait_that_already_ended_is_409(wf, monkeypatch, code):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD))
    token = _start_try(wf)
    Sim(wf.workflow, wf.sfn).start(token)
    wf.sfn.raise_on["SendTaskSuccess"] = code
    out = _try_decide(wf, token)
    assert out["statusCode"] == 409
    assert json.loads(out["body"]) == {"error": "approval is no longer pending"}
    assert wf.table.items[token]["status"] == "PENDING"


def test_a_second_decision_finds_the_token_used_and_is_409(wf, monkeypatch):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD, COMPLETED))
    token = _start_try(wf)
    Sim(wf.workflow, wf.sfn).start(token)
    assert _try_decide(wf, token, "approve")["statusCode"] == 202
    # Before the workflow applied it, the row still holds the used token.
    assert _try_decide(wf, token, "decline")["statusCode"] == 409


def test_any_other_hand_off_failure_is_503(wf, monkeypatch, capsys):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD))
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    wf.sfn.raise_on["SendTaskSuccess"] = "ThrottlingException"
    out = _try_decide(wf, token)
    assert out["statusCode"] == 503
    assert sim.waiting is not None
    assert sim.waiting[1] not in capsys.readouterr().out


def test_refusals_are_checked_before_any_send(wf, monkeypatch):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD))
    token = _start_try(wf)
    Sim(wf.workflow, wf.sfn).start(token)
    sent = len(wf.sfn.calls)
    wf.table.items[token]["expires_ts"] = int(time.time()) - 5
    out = _try_decide(wf, token)
    assert out["statusCode"] == 409 and "expired" in json.loads(out["body"])["error"]
    assert len(wf.sfn.calls) == sent
    # A pause without its token (never on the normal path) is a 503 to retry.
    wf.table.items[token]["expires_ts"] = int(time.time()) + 600
    wf.table.items[token]["task_token"] = ""
    out = _try_decide(wf, token)
    assert out["statusCode"] == 503 and len(wf.sfn.calls) == sent
    # The other lane's route never reaches the workflow.
    assert _approver_decide(wf, token, "approve")["statusCode"] == 404


def test_decisions_follow_the_row_not_the_flag(wf, monkeypatch):
    # A workflow-owned run is decided through the workflow with the flag off...
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD, PAUSE_PAYLOAD))
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    monkeypatch.setenv("USE_STEP_FUNCTIONS", "false")
    assert _try_decide(wf, token)["statusCode"] == 202
    assert wf.sfn.calls[-1][0] == "send_task_success" and wf.runner_calls == []
    assert sim.answered is not None
    # ...and a runner-owned run through the runner with the flag on.
    runner_token = _start_try(wf)
    wf.runner.start("p", runner_token, context=_Lambda("req-r"))
    _flag_on(monkeypatch)
    assert _try_decide(wf, runner_token)["statusCode"] == 202
    assert wf.table.items[runner_token]["status"] == "APPROVED"
    assert wf.runner_calls[-1] == {"action": "resume", "approval_id": runner_token}


# --- flag on: expiry, re-pause and failures ---------------------------


def test_an_undecided_try_pause_expires_without_waking_the_agent(wf, monkeypatch):
    _flag_on(monkeypatch)
    agent = Agent(PAUSE_PAYLOAD)
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    sim.time_out("States.HeartbeatTimeout")
    assert sim.trail[-3:] == ["Expire", "ExpiryOutcome", "Refused"]
    row = wf.table.items[token]
    assert row["status"] == "EXPIRED" and row["task_token"] == ""
    assert len(agent.calls) == 1
    assert _events(wf, token)[-1] == "RunExpired"
    assert _try_decide(wf, token)["statusCode"] == 409


def test_an_undecided_approval_pause_is_resumed_with_no_then_expired(wf, monkeypatch):
    _flag_on(monkeypatch)
    agent = Agent(PAUSE_PAYLOAD, EndpointConnectionError(endpoint_url="https://x.invalid"),
                  COMPLETED)
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    out = wf.api.handler(_event("POST /approvals/start", APPROVER, {"prompt": "go"}), None)
    approval_id = json.loads(out["body"])["approval_id"]
    sim = Sim(wf.workflow, wf.sfn).start(approval_id)
    sim.time_out("States.Timeout")
    assert sim.ended == "Refused"
    # A pre-send failure of the "no" is tried again in the same task.
    assert [c["interrupt_responses"][0]["response"] for c in agent.calls[1:]] == ["no", "no"]
    row = wf.table.items[approval_id]
    assert row["status"] == "EXPIRED" and row["decided_by_name"] == ""
    assert _events(wf, approval_id)[-2:] == ["RunResuming", "RunExpired"]


def test_a_decision_past_the_deadline_goes_to_expire(wf, monkeypatch):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD))
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    expires = int(wf.table.items[token]["expires_ts"])
    sim.answer(sim.waiting[1], {"decision": "approve", "decided_by": "try-token",
                                "decided_by_name": "try-visitor", "decided_ts": expires + 1})
    sim.drain()
    assert "Expire" in sim.trail and sim.ended == "Refused"
    assert wf.table.items[token]["status"] == "EXPIRED"


def test_a_resumed_run_that_pauses_again_waits_again_with_a_renewed_expiry(wf, monkeypatch):
    _flag_on(monkeypatch)
    agent = Agent(PAUSE_PAYLOAD, PAUSE_PAYLOAD, COMPLETED)
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    assert _try_decide(wf, token, "approve")["statusCode"] == 202
    sim.drain()
    assert sim.trail.count("WaitForDecision") == 2 and sim.waiting is not None
    row = wf.table.items[token]
    assert row["status"] == "PENDING" and int(row["pause_seq"]) == 2
    assert row["task_token"] == sim.waiting[1]
    heartbeat = sim.bounds[-1][0]
    assert abs(heartbeat - wf.common.TTL_SECONDS) <= 2
    assert [d["decision"] for d in row["decisions"]] == ["approve"]
    assert _try_decide(wf, token, "approve")["statusCode"] == 202
    sim.drain()
    assert sim.ended == "Completed" and wf.table.items[token]["status"] == "DONE"


def test_a_resume_that_may_have_run_ends_error_never_pending(wf, monkeypatch):
    _flag_on(monkeypatch)
    agent = Agent(PAUSE_PAYLOAD, ReadTimeoutError(endpoint_url="https://x.invalid"))
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    _try_decide(wf, token)
    sim.drain()
    assert sim.ended == "Failed"
    assert wf.table.items[token]["status"] == "ERROR"
    assert _events(wf, token)[-1] == "RunFailed"


def test_a_resume_never_sent_returns_to_the_person(wf, monkeypatch):
    _flag_on(monkeypatch)
    agent = Agent(
        PAUSE_PAYLOAD, EndpointConnectionError(endpoint_url="https://x.invalid"), COMPLETED
    )
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    expires = wf.table.items[token]["expires_ts"]
    _try_decide(wf, token)
    sim.drain()
    # Back to PENDING on the same pause and expiry, with a new token.
    row = wf.table.items[token]
    assert row["status"] == "PENDING" and row["expires_ts"] == expires
    assert row["task_token"] == sim.waiting[1]
    assert [d["decision"] for d in row["decisions"]] == ["approve_not_sent"]
    assert _try_decide(wf, token)["statusCode"] == 202
    sim.drain()
    assert sim.ended == "Completed"


def test_an_agent_error_on_start_ends_failed(wf, monkeypatch):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent({"status": "error", "error": "no"}))
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    assert sim.trail == ["AgentTurn", "TurnOutcome", "Failed"]
    assert wf.table.items[token]["status"] == "ERROR"


def test_a_completed_start_ends_completed(wf, monkeypatch):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(COMPLETED))
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    assert sim.ended == "Completed" and wf.table.items[token]["status"] == "DONE"


def test_a_crashed_task_is_recorded_as_error_by_its_name(wf, monkeypatch):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(COMPLETED))
    token = _start_try(wf)

    def _crash(*args, **kwargs):
        raise MemoryError("boom")

    monkeypatch.setattr(wf.workflow, "_start_turn", _crash)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    assert sim.trail == ["AgentTurn", "RecordFailure", "Failed"]
    row = wf.table.items[token]
    assert row["status"] == "ERROR"
    assert row["result_text"] == "MemoryError: a workflow task failed"


def test_record_failure_never_moves_a_run_the_table_forbids(wf, monkeypatch, capsys):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD))
    token = _start_try(wf)
    Sim(wf.workflow, wf.sfn).start(token)
    before = copy.deepcopy(wf.table.items[token])
    assert wf.workflow.handler({"action": "fail", "approval_id": token, "error": "X"}, None) == {
        "outcome": "failed", "heartbeat_s": 0, "timeout_s": 0}
    assert wf.table.items[token] == before
    assert '"workflow_failure_unrecorded": "PENDING"' in capsys.readouterr().out
    assert wf.workflow.handler({"action": "nope"}, None)["status"] == "error"


def test_a_wait_that_cannot_open_reports_send_task_failure(wf, monkeypatch, capsys):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD))
    token = _start_try(wf)

    real = wf.workflow.service.await_decision

    def _await_on_a_closed_run(approval_id, task_token):
        wf.table.items[approval_id]["status"] = "ERROR"
        return real(approval_id, task_token)

    monkeypatch.setattr(wf.workflow.service, "await_decision", _await_on_a_closed_run)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    assert ("send_task_failure") in wf.sfn.ops()
    assert sim.trail[-3:] == ["WaitForDecision", "RecordFailure", "Failed"]
    out = capsys.readouterr().out
    assert "tok-" not in out and '"await_failed"' in out


# --- retried invocations never run the agent twice --------------------


def test_a_retried_start_turn_reports_the_row_and_never_reinvokes(wf, monkeypatch):
    _flag_on(monkeypatch)
    agent = Agent(PAUSE_PAYLOAD)
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    token = _start_try(wf)
    first = wf.workflow.turn(token, _Lambda("req-a"))
    again = wf.workflow.turn(token, _Lambda("req-b"))
    assert first["outcome"] == again["outcome"] == "interrupt"
    assert abs(first["heartbeat_s"] - again["heartbeat_s"]) <= 1
    assert len(agent.calls) == 1


def test_a_claimed_turn_without_an_outcome_ends_error(wf, monkeypatch):
    _flag_on(monkeypatch)
    agent = Agent()
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    token = _start_try(wf)
    service = wf.workflow.service
    service.claim_run(token, "req-lost")  # an attempt that died mid-turn
    out = wf.workflow.turn(token, _Lambda("req-retry"))
    assert out["outcome"] == "failed" and agent.calls == []
    row = wf.table.items[token]
    assert row["status"] == "ERROR" and row["result_text"].startswith("TurnOutcomeUnknown")


def test_a_resume_is_claimed_once(wf, monkeypatch):
    _flag_on(monkeypatch)
    agent = Agent(PAUSE_PAYLOAD, ReadTimeoutError(endpoint_url="https://x.invalid"))
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    _try_decide(wf, token)
    name, output = sim.answered
    assert wf.workflow.apply_decision(token, output)["outcome"] == "resume"
    # A retried apply is a no-op that still answers resume.
    assert wf.workflow.apply_decision(token, output)["outcome"] == "resume"
    domain = wf.domain
    work = wf.workflow.service.unit_of_work()
    run = work.get(token)
    wf.workflow.service.claim_resume(run)  # an attempt that died mid-resume
    with pytest.raises(domain.AlreadyClaimed):
        work.get(token).claim_resume()
    out = wf.workflow.turn(token, _Lambda("req-retry"))
    assert out["outcome"] == "failed" and len(agent.calls) == 1
    assert wf.table.items[token]["result_text"].startswith("ResumeOutcomeUnknown")


def test_wait_bounds_follow_the_expiry_and_are_never_zero(wf):
    result = wf.workflow._result
    assert result("interrupt", 1_000, 400.9) == {
        "outcome": "interrupt", "heartbeat_s": 600, "timeout_s": 605}
    assert result("interrupt", 100, 500)["heartbeat_s"] == 1
    assert result("completed") == {"outcome": "completed", "heartbeat_s": 0, "timeout_s": 0}


def test_an_expiry_racing_the_wait_opens_the_staged_pause_first(wf, monkeypatch):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD))
    token = _start_try(wf)
    assert wf.workflow.turn(token, _Lambda("req-a"))["outcome"] == "interrupt"
    # The timeout fired before the wait's Lambda wrote the pause.
    assert wf.workflow.expire(token)["outcome"] == "expired"
    row = wf.table.items[token]
    assert row["status"] == "EXPIRED" and row["staged_pause"] == ""
    assert _events(wf, token)[-2:] == ["RunPaused", "RunExpired"]


# --- the sweeper leaves workflow-owned runs alone ---------------------


def test_the_sweeper_skips_workflow_owned_rows_in_every_loop(wf, monkeypatch):
    common = wf.common
    calls = []
    monkeypatch.setattr(common, "invoke_agent", lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(wf.runner, "_invoke_runner", lambda payload: calls.append(payload))
    old = int(time.time()) - 5000
    base = {"session_id": "s" * 40, "lane": "approval", "card": "{}", "interrupts": "[]",
            "orchestrator": "sfn", "created_ts": old, "expires_ts": old, "ttl": old + 99999,
            "version": 3, "pause_seq": 1, "event_seq": 3}
    rows = {
        "appr-wf-start": {"status": "STARTING", "claimed_request_id": "req-1"},
        "appr-wf-resuming": {"status": "RESUMING", "resuming_ts": old},
        "appr-wf-approved": {"status": "APPROVED", "decided_at": "2020-01-01T00:00:00+00:00"},
        "appr-wf-pending": {"status": "PENDING"},
        "appr-wf-unclaimed": {"status": "STARTING"},
    }
    for approval_id, extra in rows.items():
        wf.table.items[approval_id] = {**base, "approval_id": approval_id, **extra}
    out = wf.runner.sweep(None)
    assert calls == []
    assert out["rescued"] == out["redispatched"] == out["expired"] == 0
    assert {k: wf.table.items[k]["status"] for k in rows} == {
        "appr-wf-start": "STARTING",
        "appr-wf-resuming": "RESUMING",
        "appr-wf-approved": "APPROVED",
        "appr-wf-pending": "PENDING",
        # No execution ever claimed it, so nothing else would close it.
        "appr-wf-unclaimed": "ERROR",
    }
    assert out["closed_starting"] == 1


# --- domain and public view -------------------------------------------


def test_public_view_never_carries_workflow_attributes(wf):
    item = {
        "approval_id": "appr-v", "status": "PENDING", "lane": "try", "card": "{}",
        "orchestrator": "sfn", "prompt": "secret prompt", "task_token": "tok-secret",
        "staged_pause": "{}", "resume_claim": "4", "created_ts": 1,
    }
    view = json.dumps(wf.common.public_view(item))
    for leaked in ("sfn", "secret prompt", "tok-secret", "staged_pause", "resume_claim"):
        assert leaked not in view


def test_new_commands_stay_inside_the_transition_table(wf):
    domain = wf.domain
    run = domain.Run.from_item({"approval_id": "appr-d", "status": "PENDING", "lane": "try"})
    with pytest.raises(domain.InvalidTransition):
        run.stage_pause([], {}, 0)
    with pytest.raises(domain.InvalidTransition):
        run.claim_resume()
    done = domain.Run.from_item({"approval_id": "appr-e", "status": "DONE"})
    with pytest.raises(domain.InvalidTransition):
        done.await_decision("t", 60)
    staged = domain.Run.from_item({"approval_id": "appr-f", "status": "STARTING",
                                   "expires_ts": 500})
    staged.stage_pause([{"interruptId": "i"}], {"tool": "x"}, 100)
    with pytest.raises(domain.InvalidTransition):
        staged.stage_pause([], {}, 100)
    assert staged.next_pause_expiry(100, 60) == 500
    assert staged.status == "STARTING" and staged.events == []
    resuming = domain.Run.from_item({"approval_id": "appr-g", "status": "RESUMING"})
    assert resuming.next_pause_expiry(100, 60) == 160
    bad = domain.Run.from_item({"approval_id": "appr-h", "status": "STARTING",
                                "staged_pause": "not json"})
    assert bad.staged_pause == {}


# --- the stack --------------------------------------------------------


def _tree():
    return ast.parse(STACK.read_text(encoding="utf-8"))


def _role_statements(target: str) -> list[str]:
    return [
        ast.unparse(node.args[0])
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) in {f"{target}.add_to_role_policy", f"{target}.add_to_policy"}
    ]


def _construct(logical_id: str) -> ast.Call:
    [call] = [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call)
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == logical_id
    ]
    return call


def test_stack_grants_each_principal_only_the_state_machine_actions_it_issues():
    states = {
        target: [s for s in _role_statements(target) if "states:" in s or "lambda:" in s]
        for target in ("api_fn", "run_task", "workflow_role", "runner", "sweeper")
    }
    assert states["api_fn"] == [
        (
            "iam.PolicyStatement(actions=['states:StartExecution', 'states:SendTaskSuccess'], "
            "resources=[state_machine.attr_arn])"
        )
    ]
    assert states["run_task"] == [
        "iam.PolicyStatement(actions=['states:SendTaskFailure'], resources=[state_machine_arn])"
    ]
    assert states["workflow_role"] == [
        (
            "iam.PolicyStatement(actions=['lambda:InvokeFunction'], "
            "resources=[run_task.function_arn])"
        )
    ]
    assert states["runner"] == [] and states["sweeper"] == []
    source = STACK.read_text(encoding="utf-8")
    assert "grant_task_response" not in source and "grant_start_execution" not in source
    assert "grant_store(run_task, ('GetItem', 'UpdateItem'))" in ast.unparse(_tree())
    assert len(re.findall(r'"states:\w+"', source)) == 3


def test_stack_defines_a_standard_state_machine_from_the_pure_definition():
    kw = {k.arg: ast.unparse(k.value) for k in _construct("RunStateMachine").keywords}
    assert ast.unparse(_construct("RunStateMachine").func) == "sfn.CfnStateMachine"
    assert kw["state_machine_type"] == "'STANDARD'"
    assert kw["state_machine_name"] == "run_workflow.STATE_MACHINE_NAME"
    assert asl.STATE_MACHINE_NAME == "lachesis-run"
    assert kw["definition_string"] == "json.dumps(run_workflow.definition(), indent=2)"
    assert kw["definition_substitutions"] == (
        "{run_workflow.TASK_FUNCTION_KEY: run_task.function_arn}"
    )
    assert kw["role_arn"] == "workflow_role.role_arn"
    task = {k.arg: ast.unparse(k.value) for k in _construct("RunTask").keywords}
    assert task["handler"] == "'workflow.handler'"
    assert task["timeout"] == "cdk.Duration.minutes(15)"
    alarm = {k.arg: ast.unparse(k.value) for k in _construct("RunWorkflowFailedAlarm").keywords}
    assert "ExecutionsFailed" in alarm["metric"]
    assert "workflow_failed_alarm.add_alarm_action(cw_actions.SnsAction(" in STACK.read_text(
        encoding="utf-8")


def test_the_flag_defaults_to_false_and_reaches_the_api_only():
    source = STACK.read_text(encoding="utf-8")
    assert (
        'USE_STEP_FUNCTIONS = os.environ.get("LACHESIS_USE_STEP_FUNCTIONS", "false")' in source
    )
    assert source.count('"USE_STEP_FUNCTIONS": USE_STEP_FUNCTIONS') == 1
    api_call = ast.unparse(_construct("Api"))
    assert "'USE_STEP_FUNCTIONS': USE_STEP_FUNCTIONS" in api_call
    assert 'api_fn.add_environment("STATE_MACHINE_ARN", state_machine.attr_arn)' in source


def test_existing_logical_ids_stay_and_new_ones_are_unique():
    import test_runner_safety as safety

    constructs = safety._constructs()
    for logical_id in (*safety.EXISTING_IDS, "RunnerDlq", "RunnerDlqSentAlarm"):
        assert len(constructs.get(logical_id, [])) == 1, logical_id
    for logical_id in ("RunTask", "RunWorkflowRole", "RunStateMachine", "RunWorkflowFailedAlarm"):
        assert len(constructs.get(logical_id, [])) == 1, logical_id
    assert all(len(calls) == 1 for calls in constructs.values())


@pytest.mark.parametrize(
    "path",
    [
        "approvals/lambda/workflow.py",
        "approvals/lambda/orchestrator.py",
        "approvals/lambda/api.py",
        "approvals/lambda/approval/domain.py",
        "approvals/lambda/approval/service.py",
        "infra/stacks/run_workflow.py",
        "tests/test_run_workflow.py",
    ],
)
def test_no_em_or_en_dashes(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert chr(0x2014) not in text and chr(0x2013) not in text

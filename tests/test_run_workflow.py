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
decision a SendTaskSuccess, and the workflow writes every status except
ERROR on a failed StartExecution (the API) and the close of a row that no
live execution can still write (the sweeper, after its grace).
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


TIMEOUTS = ("States.Timeout", "States.HeartbeatTimeout")


def _matches(error, names):
    """ErrorEquals: States.TaskFailed matches any error but a timeout."""
    if error in names or "States.ALL" in names:
        return True
    return "States.TaskFailed" in names and error not in TIMEOUTS


class Sim:
    """One execution of the definition, run state by state.

    Each Task honours its Retry list: the first retrier whose ErrorEquals
    matches is used, its attempts are counted per state entry, and past
    its MaxAttempts the error falls through to Catch. A function error is
    its exception's type name (domain.Busy is "Busy"), as Lambda reports
    it. ``lost`` simulates at-least-once delivery: {state: n} lets the
    function run, then reports Lambda.ServiceException for its first n
    invocations, so the retry runs it again with a fresh request id (and,
    for the wait, a fresh task token). ``late_await`` holds the wait's
    invocation back, like a cold start, until deliver_late() runs it,
    which a test can do after the wait has already timed out."""

    def __init__(self, workflow, sfn, definition=None, lost=None, late_await=False):
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
        self.retries = []  # (state name, error) per retried attempt
        self.lost = dict(lost or {})
        self.late_await = late_await
        self.late = None  # the held-back wait payload
        self._ids = itertools.count(1)

    def start(self, approval_id):
        self.data = {"approval_id": approval_id}
        self.state = self.definition["StartAt"]
        self._run()
        return self

    def _catch(self, spec, error, cause):
        for catcher in spec.get("Catch", []):
            if _matches(error, catcher["ErrorEquals"]):
                self.data = _with_result(
                    self.data, catcher["ResultPath"], {"Error": error, "Cause": cause}
                )
                self.state = catcher["Next"]
                return
        raise AssertionError(f"uncaught {error} in {self.state}")

    def _invoke(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        return self.workflow.handler(payload, _Lambda(f"req-{next(self._ids)}"))

    def _attempt(self, name, spec):
        """One invocation: ("done", raw), ("wait", None) or ("error", (error, cause))."""
        waits = spec["Resource"].endswith(".waitForTaskToken")
        token = ""
        if waits:
            token = f"tok-{next(self._ids)}-" + "x" * 40
            self.waiting = (name, token)
            self.failed = None
        payload = _template(spec["Parameters"]["Payload"], self.data, token)
        if waits and self.late_await:
            self.late = payload
            return "wait", None
        try:
            raw = self._invoke(payload)
        except Exception as exc:
            self.waiting = None
            return "error", (type(exc).__name__, str(exc))
        if self.lost.get(name):
            self.lost[name] -= 1
            self.waiting = None
            return "error", ("Lambda.ServiceException", "the response was lost")
        if waits:
            if self.failed is not None:
                return "error", self.failed
            return "wait", None
        return "done", raw

    def _task(self, name, spec):
        """Run a Task with its retries. False while it waits for a token."""
        if spec["Resource"].endswith(".waitForTaskToken"):
            self.bounds.append(
                (
                    _path(spec["HeartbeatSecondsPath"], self.data),
                    _path(spec["TimeoutSecondsPath"], self.data),
                )
            )
        retry = spec.get("Retry", [])
        attempts = [0] * len(retry)
        while True:
            kind, value = self._attempt(name, spec)
            if kind == "wait":
                return False
            if kind == "done":
                result = {"Payload": value}
                if "ResultSelector" in spec:
                    result = _template(spec["ResultSelector"], result)
                self.data = _with_result(self.data, spec.get("ResultPath", "$"), result)
                self.state = spec["Next"]
                return True
            error, cause = value
            index = next(
                (i for i, rule in enumerate(retry) if _matches(error, rule["ErrorEquals"])), None
            )
            if index is not None and attempts[index] < retry[index]["MaxAttempts"]:
                attempts[index] += 1
                self.retries.append((name, error))
                continue
            self._catch(spec, error, cause)
            return True

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
            if not self._task(name, spec):
                return  # waits for answer() or time_out()

    def deliver_late(self):
        """The held-back wait invocation finally runs (its result is moot
        once the wait has ended)."""
        payload, self.late = self.late, None
        return self._invoke(payload)

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
    forbidden = {"States.ALL", "Lambda.ClientExecutionTimeoutException", "States.Timeout"}
    # Only the tasks that never invoke the agent retry their own failures.
    idempotent = {"ApplyDecision", "RecordFailure"}
    for name, spec in tasks.items():
        assert spec["Parameters"]["FunctionName"] == "${TaskFunctionArn}", name
        assert spec["Parameters"]["Payload"]["approval_id.$"] == "$.approval_id"
        assert spec["Parameters"]["Payload"]["action"] in asl_actions(), name
        retry, *own = spec["Retry"]
        assert retry["ErrorEquals"] == [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException",
        ], name
        if name in idempotent:
            assert [r["ErrorEquals"] for r in own] == [["States.TaskFailed"]], name
        else:
            assert own == [], name
        for rule in spec["Retry"]:
            assert not forbidden & set(rule["ErrorEquals"])
            assert rule["MaxAttempts"] <= 3
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


# --- the sweeper: hands off while an execution can be live -----------


def _workflow_rows(w, moment, lane="approval"):
    base = {"session_id": "s" * 40, "lane": lane, "card": "{}", "interrupts": "[]",
            "orchestrator": "sfn", "created_ts": moment, "expires_ts": moment,
            "ttl": moment + 99999, "version": 3, "pause_seq": 1, "event_seq": 3}
    decided = w.domain.iso(moment)
    rows = {
        "appr-wf-start": {"status": "STARTING", "claimed_request_id": "req-1"},
        "appr-wf-resuming": {"status": "RESUMING", "resuming_ts": moment,
                             "resumed_decision": "APPROVED"},
        "appr-wf-approved": {"status": "APPROVED", "decided_at": decided},
        "appr-wf-declined": {"status": "DECLINED", "decided_at": decided},
        "appr-wf-pending": {"status": "PENDING", "task_token": "tok-dead"},
        "appr-wf-unclaimed": {"status": "STARTING"},
    }
    for approval_id, extra in rows.items():
        w.table.items[approval_id] = {**base, "approval_id": approval_id, **extra}
    return rows


def test_the_sweeper_leaves_workflow_owned_rows_alone_inside_their_grace(wf, monkeypatch):
    calls = []
    monkeypatch.setattr(wf.common, "invoke_agent", lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(wf.runner, "_invoke_runner", lambda payload: calls.append(payload))
    # Past every runner-path rule (expiry, REDISPATCH_AFTER_S), inside
    # every workflow grace: an execution may still be live.
    recent = int(time.time()) - 200
    rows = _workflow_rows(wf, recent)
    out = wf.runner.sweep(None)
    assert calls == []
    assert out["rescued"] == out["redispatched"] == out["expired"] == 0
    assert out["closed_starting"] == out["closed_decided"] == 0
    assert {k: wf.table.items[k]["status"] for k in rows} == {
        k: extra["status"] for k, extra in rows.items()
    }
    assert wf.table.items["appr-wf-pending"]["task_token"] == "tok-dead"


def test_the_sweeper_closes_workflow_owned_rows_past_their_grace(wf, monkeypatch):
    agent = Agent(COMPLETED)
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    dispatched = []
    monkeypatch.setattr(wf.runner, "_invoke_runner", lambda payload: dispatched.append(payload))
    old = int(time.time()) - 5000
    rows = _workflow_rows(wf, old)
    wf.table.items["try-wf-pending"] = {
        **wf.table.items["appr-wf-pending"], "approval_id": "try-wf-pending", "lane": "try"}
    out = wf.runner.sweep(None)
    # A resume is never re-dispatched for a workflow-owned run.
    assert dispatched == [] and out["redispatched"] == 0
    assert {k: wf.table.items[k]["status"] for k in (*rows, "try-wf-pending")} == {
        "appr-wf-start": "ERROR",
        "appr-wf-resuming": "ERROR",
        "appr-wf-approved": "ERROR",
        "appr-wf-declined": "ERROR",
        # The approval lane still gets its "no"; the /try lane never wakes the agent.
        "appr-wf-pending": "EXPIRED",
        "try-wf-pending": "EXPIRED",
        "appr-wf-unclaimed": "ERROR",
    }
    assert [c["interrupt_responses"] for c in agent.calls] == [[]]
    assert out["closed_starting"] == 2 and out["rescued"] == 1
    assert out["closed_decided"] == 2 and out["closed_try"] == 1
    for approval_id in ("appr-wf-pending", "try-wf-pending"):
        assert wf.table.items[approval_id]["task_token"] == ""
    # A decided run moves through RESUMING to ERROR in one write; the agent
    # was not invoked for it.
    assert _events(wf, "appr-wf-approved") == ["RunResuming", "RunFailed"]
    assert wf.table.items["appr-wf-approved"]["result_text"].startswith("WorkflowLost")


def test_the_sweeper_graces_outlast_every_task_of_the_definition(wf):
    runner, states = wf.runner, _states()

    def intervals(spec):
        return sum(
            rule["IntervalSeconds"] * rule["BackoffRate"] ** k
            for rule in spec["Retry"]
            for k in range(rule["MaxAttempts"])
        )

    agent_turn, expire, apply = states["AgentTurn"], states["Expire"], states["ApplyDecision"]
    assert runner.WORKFLOW_TASK_TIMEOUT_S == asl.AGENT_TASK_TIMEOUT_S
    assert agent_turn["TimeoutSeconds"] == expire["TimeoutSeconds"] == asl.AGENT_TASK_TIMEOUT_S
    assert wf.workflow.WAIT_GRACE_S == runner.WORKFLOW_WAIT_GRACE_S
    assert runner.WORKFLOW_RETRY_INTERVALS_S >= intervals(agent_turn) == intervals(expire)
    # PENDING: the wait ends at the expiry, then one Expire with its retries.
    assert (
        runner.WORKFLOW_WAIT_GRACE_S + expire["TimeoutSeconds"] + intervals(expire)
    ) < runner.WORKFLOW_PENDING_GRACE_S
    # STARTING and RESUMING: one AgentTurn (or Expire) with its retries.
    for timeout in (runner.STARTING_TIMEOUT_S, runner.RESUMING_TIMEOUT_S):
        assert timeout > agent_turn["TimeoutSeconds"] + intervals(agent_turn)
    # APPROVED or DECLINED: every ApplyDecision attempt but the last ends
    # inside its task timeout; the last may run on to the Lambda limit.
    attempts = 1 + sum(rule["MaxAttempts"] for rule in apply["Retry"])
    lambda_limit_s = 900
    assert (
        (attempts - 1) * apply["TimeoutSeconds"] + intervals(apply) + lambda_limit_s
    ) < runner.WORKFLOW_DECIDED_GRACE_S


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
    for logical_id in (
        "RunTask",
        "RunWorkflowRole",
        "RunStateMachine",
        "RunWorkflowFailedAlarm",
        "RunWorkflowAbortedAlarm",
        "RunWorkflowTimedOutAlarm",
    ):
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


def test_contention_on_apply_is_raised_for_the_retry_and_a_lost_race_still_resumes(
    wf, monkeypatch
):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD))
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    _try_decide(wf, token)
    _, output = sim.answered
    # Lasting contention: raised, so States.TaskFailed retries the task.
    for _ in range(4):
        wf.table.dynamo.inject.append(["TransactionConflict"])
    with pytest.raises(wf.domain.Busy):
        wf.workflow.apply_decision(token, output)
    assert wf.table.items[token]["status"] == "PENDING"
    # A duplicate apply that lost the race to the first one still resumes.
    real = wf.workflow.service.record_decision

    def _lose(*args, **kwargs):
        real(*args, **kwargs)
        raise wf.domain.Conflict()

    monkeypatch.setattr(wf.workflow.service, "record_decision", _lose)
    assert wf.workflow.apply_decision(token, output)["outcome"] == "resume"
    assert wf.table.items[token]["status"] == "RESUMING"


# --- Expire racing the wait's own Lambda ------------------------------


def _start_lane(w, lane):
    if lane == "try":
        return _start_try(w)
    out = w.api.handler(_event("POST /approvals/start", APPROVER, {"prompt": "go"}), None)
    assert out["statusCode"] == 202, out
    return json.loads(out["body"])["approval_id"]


def _decide_lane(w, lane, approval_id):
    if lane == "try":
        return _try_decide(w, approval_id)
    return _approver_decide(w, approval_id, "approve")


def _once_before(monkeypatch, target, name, action):
    """Run ``action`` once, just before the first call of target.name, so
    a concurrent commit lands between Expire's load and its own write."""
    real = getattr(target, name)
    fired = []

    def hooked(*args, **kwargs):
        if not fired:
            fired.append(True)
            action()
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, hooked)
    return fired


@pytest.mark.parametrize("lane", ["try", "approval"])
def test_expire_retries_when_a_late_wait_opens_the_staged_pause_first(wf, monkeypatch, lane):
    _flag_on(monkeypatch)
    agent = Agent(PAUSE_PAYLOAD, COMPLETED)
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    approval_id = _start_lane(wf, lane)
    # A cold-started wait Lambda: the heartbeat fires before it commits.
    sim = Sim(wf.workflow, wf.sfn, late_await=True).start(approval_id)
    assert wf.table.items[approval_id]["staged_pause"] != ""
    real_await = wf.domain.Run.await_decision
    delivered = []

    def await_decision(run, task_token, ttl_seconds):
        # Expire opens the stage with no token; the late Lambda commits
        # the pause and its token first, so Expire's commit conflicts.
        if task_token == "" and sim.late is not None:
            delivered.append(sim.deliver_late())
            assert wf.table.items[approval_id]["task_token"].startswith("tok-")
        return real_await(run, task_token, ttl_seconds)

    monkeypatch.setattr(wf.domain.Run, "await_decision", await_decision)
    sim.time_out("States.HeartbeatTimeout")
    assert delivered == [{"status": "waiting"}]
    assert sim.trail[-3:] == ["Expire", "ExpiryOutcome", "Refused"]
    assert sim.data["expiry"] == {"outcome": "expired"}
    row = wf.table.items[approval_id]
    assert row["status"] == "EXPIRED" and row["task_token"] == "" and row["staged_pause"] == ""
    answers = [c["interrupt_responses"][0]["response"] for c in agent.calls[1:]]
    assert answers == ([] if lane == "try" else ["no"])
    assert _events(wf, approval_id)[-1] == "RunExpired"


@pytest.mark.parametrize("lane", ["try", "approval"])
def test_expire_retries_when_a_late_wait_sets_the_token_on_a_pending_row(
    wf, monkeypatch, lane
):
    _flag_on(monkeypatch)
    agent = Agent(
        PAUSE_PAYLOAD, EndpointConnectionError(endpoint_url="https://x.invalid"), COMPLETED
    )
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    approval_id = _start_lane(wf, lane)
    sim = Sim(wf.workflow, wf.sfn).start(approval_id)
    assert _decide_lane(wf, lane, approval_id)["statusCode"] == 202
    # The resume fails before it is sent: back to PENDING with no token,
    # and the next wait's Lambda is slow.
    sim.late_await = True
    sim.drain()
    row = wf.table.items[approval_id]
    assert row["status"] == "PENDING" and row["task_token"] == "" and sim.late is not None
    command = "expire" if lane == "try" else "expiry_resume"
    fired = _once_before(monkeypatch, wf.workflow.service, command, sim.deliver_late)
    sim.time_out("States.HeartbeatTimeout")
    assert fired and sim.ended == "Refused"
    row = wf.table.items[approval_id]
    assert row["status"] == "EXPIRED" and row["task_token"] == ""
    # The approval lane's "no" is sent exactly once; /try never wakes the agent.
    assert len(agent.calls) == (2 if lane == "try" else 3)


def test_an_expire_that_loses_every_race_fails_and_the_sweeper_closes_the_row(
    wf, monkeypatch, capsys
):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD))
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    real_expire = wf.workflow.service.expire
    lost = []

    def always_lose(run):
        lost.append(run.version)
        raise wf.domain.Conflict()

    monkeypatch.setattr(wf.workflow.service, "expire", always_lose)
    sim.time_out("States.HeartbeatTimeout")
    # Never reported as an open interrupt: FAILED, and the execution fails.
    assert sim.data["expiry"] == {"outcome": "failed"} and sim.ended == "Failed"
    assert len(lost) == wf.workflow.EXPIRE_ATTEMPTS
    assert '"expiry_unrecorded": "Conflict"' in capsys.readouterr().out
    row = wf.table.items[token]
    assert row["status"] == "PENDING"
    monkeypatch.setattr(wf.workflow.service, "expire", real_expire)
    # Inside the grace the sweeper leaves it; past it, the row ends EXPIRED.
    assert wf.runner.sweep(None)["closed_try"] == 0
    row["expires_ts"] = int(time.time()) - wf.runner.WORKFLOW_PENDING_GRACE_S - 1
    assert wf.runner.sweep(None)["closed_try"] == 1
    assert wf.table.items[token]["status"] == "EXPIRED"
    assert wf.table.items[token]["task_token"] == ""


# --- at-least-once task delivery, through the definition --------------


@pytest.mark.parametrize("state", ["AgentTurn", "WaitForDecision", "ApplyDecision"])
def test_a_lost_task_response_is_retried_and_the_run_still_completes(wf, monkeypatch, state):
    _flag_on(monkeypatch)
    agent = Agent(PAUSE_PAYLOAD, COMPLETED)
    monkeypatch.setattr(wf.common, "invoke_agent", agent)
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn, lost={state: 1}).start(token)
    # The row holds the token of the wait that is open now.
    assert wf.table.items[token]["task_token"] == sim.waiting[1]
    if state == "WaitForDecision":
        assert len([p for p in sim.payloads if p["action"] == "await"]) == 2
    assert _try_decide(wf, token)["statusCode"] == 202
    sim.drain()
    assert sim.retries == [(state, "Lambda.ServiceException")]
    assert sim.ended == "Completed" and wf.table.items[token]["status"] == "DONE"
    # The retry never ran the agent again, nor resumed twice.
    assert len(agent.calls) == 2
    assert _events(wf, token).count("RunResuming") == 1


def test_contention_on_apply_is_retried_by_the_definition_and_the_run_resumes(wf, monkeypatch):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD, COMPLETED))
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    assert _try_decide(wf, token)["statusCode"] == 202
    retries = sys.modules["approval.uow"].CONTENTION_RETRIES
    # Two applies in a row run out of contention retries (domain.Busy).
    wf.table.dynamo.inject = [["TransactionConflict", "None"]] * (2 * (retries + 1))
    sim.drain()
    assert sim.retries == [("ApplyDecision", "Busy")] * 2
    assert sim.trail[-5:] == [
        "ApplyDecision", "DecisionOutcome", "AgentTurn", "TurnOutcome", "Completed"]
    assert wf.table.items[token]["status"] == "DONE"


def test_contention_past_the_apply_retries_fails_and_the_sweeper_closes_the_row(
    wf, monkeypatch
):
    _flag_on(monkeypatch)
    monkeypatch.setattr(wf.common, "invoke_agent", Agent(PAUSE_PAYLOAD))
    token = _start_try(wf)
    sim = Sim(wf.workflow, wf.sfn).start(token)
    assert _try_decide(wf, token)["statusCode"] == 202
    retries = sys.modules["approval.uow"].CONTENTION_RETRIES
    wf.table.dynamo.inject = [["TransactionConflict", "None"]] * (4 * (retries + 1))
    sim.drain()
    assert sim.retries == [("ApplyDecision", "Busy")] * 3
    assert sim.trail[-3:] == ["ApplyDecision", "RecordFailure", "Failed"]
    row = wf.table.items[token]
    assert row["status"] == "PENDING"
    row["expires_ts"] = int(time.time()) - wf.runner.WORKFLOW_PENDING_GRACE_S - 1
    wf.runner.sweep(None)
    assert wf.table.items[token]["status"] == "EXPIRED"


# --- a start the server accepted but the client never heard about -----


def test_a_start_that_an_execution_already_claimed_answers_202(wf, monkeypatch, capsys):
    _flag_on(monkeypatch)

    def accepted_but_lost(approval_id):
        # The execution started and its AgentTurn claimed the run, but
        # every client attempt timed out.
        assert wf.workflow.service.claim_run(approval_id, "req-exec") is not None
        raise ReadTimeoutError(endpoint_url="https://states.invalid")

    monkeypatch.setattr(wf.orchestrator, "start", accepted_but_lost)
    out = _post_try(wf.api, "198.51.100.9")
    assert out["statusCode"] == 202, out
    body = json.loads(out["body"])
    row = wf.table.items[body["token"]]
    assert body == {"token": row["approval_id"], "status": "STARTING"}
    assert row["status"] == "STARTING" and row["claimed_request_id"] == "req-exec"
    assert '"workflow_start_failed": "ReadTimeoutError"' in capsys.readouterr().out


def test_a_failed_start_whose_error_write_loses_is_still_502_when_unclaimed(wf, monkeypatch):
    _flag_on(monkeypatch)
    wf.sfn.raise_on["StartExecution"] = "ServiceUnavailable"

    def busy(run, text):
        raise wf.domain.Busy()

    monkeypatch.setattr(wf.api.service, "record_failure", busy)
    out = _post_try(wf.api, "198.51.100.9")
    assert out["statusCode"] == 502
    [row] = [r for r in wf.table.items.values() if r.get("lane") == "try"]
    # Nobody owns it: it stays STARTING, unclaimed, for the sweeper.
    assert row["status"] == "STARTING" and not row.get("claimed_request_id")


# --- the stack, B6 review ---------------------------------------------


def test_the_workflow_role_trusts_only_this_state_machine():
    role = ast.unparse(_construct("RunWorkflowRole"))
    assert "'aws:SourceAccount': self.account" in role
    assert "'ArnLike': {'aws:SourceArn': [state_machine_arn, workflow_execution_arns]}" in role
    assert "attr_arn" not in role
    source = STACK.read_text(encoding="utf-8")
    assert (
        'f"arn:aws:states:{self.region}:{self.account}:execution:"\n'
        '            f"{run_workflow.STATE_MACHINE_NAME}:*"'
    ) in source
    # Both literal ARNs are defined before the role (no cycle).
    role_at = source.index('"RunWorkflowRole"')
    assert source.index("state_machine_arn = (") < role_at
    assert source.index("workflow_execution_arns = (") < role_at


@pytest.mark.parametrize(
    ("logical_id", "metric"),
    [
        ("RunWorkflowFailedAlarm", "ExecutionsFailed"),
        ("RunWorkflowAbortedAlarm", "ExecutionsAborted"),
        ("RunWorkflowTimedOutAlarm", "ExecutionsTimedOut"),
    ],
)
def test_every_unfinished_execution_alarms_on_the_alerts_topic(logical_id, metric):
    call = _construct(logical_id)
    assert ast.unparse(call.func) == "cloudwatch.Alarm"
    kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert f"metric_name='{metric}'" in kw["metric"]
    assert "'StateMachineArn': state_machine.attr_arn" in kw["metric"]
    assert kw["threshold"] == "1"
    [name] = [
        node.targets[0].id
        for node in ast.walk(_tree())
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value) == ast.unparse(call)
    ]
    assert f"{name}.add_alarm_action(cw_actions.SnsAction(foundation.alerts_topic))" in (
        STACK.read_text(encoding="utf-8")
    )


def _ses_helper():
    [node] = [
        n for n in _tree().body
        if isinstance(n, ast.FunctionDef) and n.name == "ses_identity_arns"
    ]
    namespace = {}
    # Our own stack source, parsed offline: CDK is not installed here.
    exec("import os\nfrom email.utils import parseaddr\n" + ast.unparse(node), namespace)  # noqa: S102
    return namespace["ses_identity_arns"]


def test_ses_grants_name_exact_identities_and_none_without_a_sender(monkeypatch):
    helper = _ses_helper()
    monkeypatch.delenv("LACHESIS_SES_FROM", raising=False)
    monkeypatch.setenv("LACHESIS_SES_TO", "ops@example.org")
    assert helper("123456789012") == []
    monkeypatch.setenv("LACHESIS_SES_FROM", "Lachesis <guard@example.com>")
    assert helper("123456789012") == [
        "arn:aws:ses:eu-west-1:123456789012:identity/guard@example.com",
        "arn:aws:ses:eu-west-1:123456789012:identity/example.com",
        "arn:aws:ses:eu-west-1:123456789012:identity/ops@example.org",
        "arn:aws:ses:eu-west-1:123456789012:identity/example.org",
    ]
    assert "identity/*" not in STACK.read_text(encoding="utf-8")
    # Each SES statement is added only under `if ses_identities:`.
    guarded = [
        ast.unparse(inner.value)
        for node in ast.walk(_tree())
        if isinstance(node, ast.If) and ast.unparse(node.test) == "ses_identities"
        for inner in node.body
        if isinstance(inner, ast.Expr)
    ]
    assert guarded == [
        f"{target}.add_to_role_policy(iam.PolicyStatement(actions=['ses:SendEmail'], "
        "resources=ses_identities))"
        for target in ("runner", "run_task")
    ]
    every = [s for t in ("runner", "run_task") for s in _role_statements(t) if "ses:" in s]
    assert len(every) == 2

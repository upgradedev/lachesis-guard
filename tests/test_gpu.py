"""PHASE 2b: GET /status and POST /wake (approvals/lambda/gpu.py), offline.

SageMaker is a stub; the daily counters run on an in-memory table that
implements the one conditional update common.counter_ok uses. The console
half is structural: POST /try has one call site and every path to it is
gated on GET /status reading "awake".
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
LAMBDA_DIR = ROOT / "approvals" / "lambda"
sys.path.insert(0, str(LAMBDA_DIR))


class CounterTable:
    def __init__(self):
        self.counts: dict[str, int] = {}

    def update_item(self, Key, ExpressionAttributeValues, **_kwargs):  # noqa: N803
        key = Key["approval_id"]
        current = self.counts.get(key, 0)
        if current >= int(ExpressionAttributeValues[":cap"]):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.counts[key] = current + 1
        return {"Attributes": {"count": current + 1}}


class StubSageMaker:
    def __init__(self, desired=0, current=0, fail=False):
        self.desired, self.current, self.fail = desired, current, fail
        self.describes = 0
        self.updates: list[dict] = []

    def describe_inference_component(self, InferenceComponentName):  # noqa: N803
        assert InferenceComponentName == "lachesis-decider-2b"
        self.describes += 1
        if self.fail:
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "Describe")
        runtime = {"DesiredCopyCount": self.desired, "CurrentCopyCount": self.current}
        return {"RuntimeConfig": runtime}

    def update_inference_component_runtime_config(  # noqa: N803
        self, InferenceComponentName, DesiredRuntimeConfig
    ):
        self.updates.append({"name": InferenceComponentName, **DesiredRuntimeConfig})
        self.desired = DesiredRuntimeConfig["CopyCount"]


@pytest.fixture()
def gpu(monkeypatch):
    spec = importlib.util.spec_from_file_location("common", LAMBDA_DIR / "common.py")
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    table = CounterTable()
    monkeypatch.setattr(common, "_table", table)
    spec = importlib.util.spec_from_file_location("gpu", LAMBDA_DIR / "gpu.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "common", common)
    stub = StubSageMaker()
    monkeypatch.setattr(module, "_sagemaker", lambda: stub)
    return module, stub, common, table


def _call(module, route, ip="203.0.113.5"):
    event = {"routeKey": route, "requestContext": {"http": {"sourceIp": ip}}}
    out = module.handler(event, None)
    return out["statusCode"], json.loads(out["body"]), out["headers"]


@pytest.mark.parametrize(
    ("desired", "current", "state"),
    [(0, 0, "asleep"), (1, 0, "waking"), (1, 1, "awake"), (0, 1, "asleep"), (2, 1, "awake")],
)
def test_state_of(gpu, desired, current, state):
    module, *_ = gpu
    described = {"RuntimeConfig": {"DesiredCopyCount": desired, "CurrentCopyCount": current}}
    assert module.state_of(described) == state


def test_status_returns_only_the_state_and_caches_15_seconds(gpu, monkeypatch):
    module, stub, *_ = gpu
    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    stub.desired, stub.current = 1, 1
    status, body, headers = _call(module, "GET /status")
    assert (status, body) == (200, {"state": "awake"})
    assert headers["cache-control"] == "max-age=15"
    stub.desired, stub.current = 0, 0
    clock[0] += 14
    assert _call(module, "GET /status")[1] == {"state": "awake"}  # served from the cache
    assert stub.describes == 1
    clock[0] += 1
    assert _call(module, "GET /status")[1] == {"state": "asleep"}
    assert stub.describes == 2


def test_status_is_503_when_sagemaker_fails(gpu):
    module, stub, *_ = gpu
    stub.fail = True
    status, body, headers = _call(module, "GET /status")
    assert status == 503 and "state" not in body
    assert headers["cache-control"] == "no-store"


def test_wake_asks_for_one_copy_only_when_asleep(gpu):
    module, stub, *_ = gpu
    assert _call(module, "POST /wake")[:2] == (202, {"state": "waking"})
    assert stub.updates == [{"name": "lachesis-decider-2b", "CopyCount": 1}]
    # Idempotent: waking or awake changes nothing.
    assert _call(module, "POST /wake")[:2] == (202, {"state": "waking"})
    stub.current = 1
    assert _call(module, "POST /wake")[:2] == (202, {"state": "awake"})
    assert len(stub.updates) == 1


def test_wake_clears_the_status_cache(gpu, monkeypatch):
    module, stub, *_ = gpu
    monkeypatch.setattr(module.time, "time", lambda: 5000.0)
    assert _call(module, "GET /status")[1] == {"state": "asleep"}
    _call(module, "POST /wake")
    assert _call(module, "GET /status")[1] == {"state": "waking"}


def test_wake_is_503_when_sagemaker_fails(gpu):
    module, stub, *_ = gpu
    stub.fail = True
    assert _call(module, "POST /wake")[0] == 503
    assert stub.updates == []


def test_wake_caps_per_address_and_per_day_on_their_own_rows(gpu, monkeypatch):
    module, stub, common, table = gpu
    monkeypatch.setattr(common, "TRY_IP_DAILY_CAP", 2)
    monkeypatch.setattr(common, "TRY_DAILY_CAP", 3)
    assert _call(module, "POST /wake", ip="198.51.100.1")[0] == 202
    assert _call(module, "POST /wake", ip="198.51.100.1")[0] == 202
    status, body, _ = _call(module, "POST /wake", ip="198.51.100.1")
    assert status == 429 and "00:00 UTC" in body["error"]
    assert _call(module, "POST /wake", ip="198.51.100.2")[0] == 202
    assert _call(module, "POST /wake", ip="198.51.100.3")[0] == 429  # the daily cap
    assert all(key.startswith("wake-") for key in table.counts)
    # The /try counters are untouched: a wake never spends a visitor's runs.
    assert common.daily_caps_ok({"requestContext": {"http": {"sourceIp": "198.51.100.1"}}}, "try")


def test_unknown_route_is_404(gpu):
    module, *_ = gpu
    assert _call(module, "GET /wake")[0] == 404


def test_gpu_role_holds_only_the_two_actions_on_the_one_component():
    stack = (ROOT / "infra" / "stacks" / "approvals.py").read_text(encoding="utf-8")
    assert '("GET /status", "NONE", gpu_integration)' in stack
    assert '("POST /wake", "NONE", gpu_integration)' in stack
    assert re.findall(r'"sagemaker:[A-Za-z]+"', stack) == [
        '"sagemaker:DescribeInferenceComponent"',
        '"sagemaker:UpdateInferenceComponentRuntimeConfig"',
    ]
    assert "inference-component/lachesis-decider-2b" in stack
    assert 'table.grant(gpu_fn, "dynamodb:UpdateItem")' in stack
    assert '"POST /wake": {"ThrottlingRateLimit"' in stack


def test_console_never_posts_try_unless_status_reads_awake():
    js = (ROOT / "approvals" / "console" / "app.js").read_text(encoding="utf-8")
    sites = [m.start() for m in re.finditer(r'api\("/try", \{ method: "POST"', js)]
    assert len(sites) == 1
    assert js.rfind("async function startRun()", 0, sites[0]) > js.rfind("\nfunction ", 0, sites[0])
    calls = [
        line for line in js.splitlines()
        if "startRun()" in line and "function startRun" not in line
    ]
    assert calls and all('st === "awake"' in line for line in calls), calls
    assert 'api("/status")' in js and 'api("/wake", { method: "POST"' in js
    assert "const WAKE_MINUTES = null;" in js  # no invented wake time before it is measured
    assert not re.search(r"about \d+ minutes", js)

"""The warm script waits 45 minutes and resets a stalled copy once."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "set_decider_warm.py"


def _load():
    spec = importlib.util.spec_from_file_location("set_decider_warm", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class _FakeSm:
    """Instances come up at t+60; copies only place after a reset."""

    def __init__(self, clock, place_after_reset=True):
        self.clock = clock
        self.place_after_reset = place_after_reset
        self.reset_calls = []
        self._reset_seen = False

    def describe_endpoint(self, EndpointName):
        up = 1 if self.clock.t >= 1_700_000_060 else 0
        return {"ProductionVariants": [{"CurrentInstanceCount": up}]}

    def describe_inference_component(self, InferenceComponentName):
        current = 1 if (self._reset_seen and self.place_after_reset) else 0
        return {"RuntimeConfig": {"DesiredCopyCount": 1, "CurrentCopyCount": current}}

    def update_inference_component_runtime_config(self, **kwargs):
        self.reset_calls.append(kwargs["DesiredRuntimeConfig"]["CopyCount"])
        if kwargs["DesiredRuntimeConfig"]["CopyCount"] == 1:
            self._reset_seen = True


class _FakeAas:
    def __init__(self):
        self.targets = []

    def register_scalable_target(self, **kwargs):
        self.targets.append(kwargs)


def test_warm_resets_a_stalled_copy_once():
    module = _load()
    clock = _Clock()
    sm = _FakeSm(clock)
    out = module.warm(sm, _FakeAas(), deadline=2700, stall_after=600,
                      poll=20, now=clock.now, sleep=clock.sleep)
    assert sm.reset_calls == [0, 1]
    assert out["reset_at"] is not None
    assert out["elapsed_s"] >= 660


def test_warm_without_stall_never_resets():
    module = _load()
    clock = _Clock()

    class QuickSm(_FakeSm):
        def describe_inference_component(self, InferenceComponentName):
            current = 1 if self.clock.t >= 1_700_000_120 else 0
            return {"RuntimeConfig": {"DesiredCopyCount": 1, "CurrentCopyCount": current}}

    sm = QuickSm(clock)
    out = module.warm(sm, _FakeAas(), deadline=2700, stall_after=600,
                      poll=20, now=clock.now, sleep=clock.sleep)
    assert sm.reset_calls == []
    assert out["reset_at"] is None


def test_warm_times_out_at_the_deadline():
    module = _load()
    clock = _Clock()
    sm = _FakeSm(clock, place_after_reset=False)
    try:
        module.warm(sm, _FakeAas(), deadline=900, stall_after=600,
                    poll=20, now=clock.now, sleep=clock.sleep)
    except SystemExit as exc:
        assert "900" in str(exc)
    else:
        raise AssertionError("warm must give up at the deadline")
    assert module.DEADLINE_SECONDS >= 2700

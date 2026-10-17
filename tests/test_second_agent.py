"""The second agent (database janitor) is governed by the same pattern."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "second_agent" / "agent.py"


def _load():
    spec = importlib.util.spec_from_file_location("second_agent", _EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _answers(risk, confidence, grounded_p):
    return {
        "risk_class": {"choice": risk, "confidence": confidence},
        "grounded": {"noul": grounded_p},
    }


def _event(module, name, args, user_text):
    return SimpleNamespace(
        tool_use={"name": name, "input": args, "toolUseId": "x"},
        agent=SimpleNamespace(messages=[{"role": "user", "content": [{"text": user_text}]}]),
    )


def test_janitor_drop_pauses_and_read_proceeds():
    module = _load()

    def scripted(state, questions):
        if "drop_table" in state:
            return _answers("destructive_write", 0.91, 0.9), 3.0
        return _answers("benign", 0.95, 0.96), 3.0

    handler = module.build_handler(ask=scripted)
    from strands.interventions import Confirm, Proceed

    read = asyncio.run(handler.before_tool_call(
        _event(module, "run_query", {"table": "sessions_2024"},
               "List what is in sessions_2024, then drop sessions_2024.")
    ))
    assert isinstance(read, Proceed)
    drop = asyncio.run(handler.before_tool_call(
        _event(module, "drop_table", {"table": "sessions_2024"},
               "List what is in sessions_2024, then drop sessions_2024.")
    ))
    assert isinstance(drop, Confirm)
    # the pause is the whole point: the table survived triage
    assert "sessions_2024" in module.TABLES


def test_build_agent_registers_decorated_tools():
    module = _load()
    agent, triage = module.build_agent(
        ask=lambda s, q: (_answers("benign", 0.95, 0.96), 3.0),
        model="us.amazon.nova-lite-v1:0",
    )
    assert set(agent.tool_names) == {"run_query", "drop_table"}
    assert triage.name == "lachesis-guard"
    assert "sessions_2024: [1, 2]" in module.run_query(table="sessions_2024")
    assert module.run_query(table="missing") == "no such table: missing"

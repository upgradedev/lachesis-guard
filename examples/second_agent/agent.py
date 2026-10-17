"""A second agent on the same Lachesis pattern: a database janitor.

This is the transfer proof for the reusable pattern. Nothing here is
refund-shaped: a tiny Strands agent with a read tool and a destructive
tool, governed by the shipped pattern handler loaded from
patterns/guardrails/triage.py, copied the way the pattern README tells
a builder to copy it. Three lines wire it in; the questions, the
threshold, the deterministic pre-checks and the pause all come from
the pattern file.

Run it (needs a Decider server and Bedrock credentials):

    python examples/second_agent/agent.py

Offline, the wiring is pinned by tests/test_second_agent.py, which
drives build_handler with scripted Decider answers.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from strands import Agent, tool
from strands.models import BedrockModel

_PATTERN = Path(__file__).resolve().parents[2] / "patterns" / "guardrails" / "triage.py"
MODEL_ID = os.environ.get("MODEL_ID", "eu.anthropic.claude-sonnet-5-5")


def _load_pattern():
    spec = importlib.util.spec_from_file_location("lachesis_pattern", _PATTERN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pattern = _load_pattern()

# An in-memory toy database: the tools are real functions with real
# side effects on this dict, so a governed call that proceeds is
# visible and one that pauses is not.
TABLES: dict[str, list[dict]] = {
    "sessions_2024": [{"id": 1}, {"id": 2}],
    "sessions_2025": [{"id": 3}],
}


@tool
def run_query(table: str) -> str:
    """List the row ids in a table (read-only)."""
    rows = TABLES.get(table)
    if rows is None:
        return f"no such table: {table}"
    return f"{table}: {[r['id'] for r in rows]}"


@tool
def drop_table(table: str) -> str:
    """Delete a table and all its rows. Destructive."""
    if table not in TABLES:
        return f"no such table: {table}"
    del TABLES[table]
    return f"dropped {table}"


TOOLS = [run_query, drop_table]


def build_handler(ask=None):
    """The three-line wiring from the pattern README, on this agent.

    ask is injectable so tests (and builders) can drive the handler
    with a scripted Decider instead of a server.
    """
    return pattern.TriageHandler(ask=ask) if ask is not None else pattern.TriageHandler()


def build_agent(ask=None, model=None):
    triage = build_handler(ask)
    agent = Agent(
        model=model or BedrockModel(model_id=MODEL_ID),
        tools=TOOLS,
        interventions=[triage],
        system_prompt=(
            "You are a database janitor. You may inspect tables with "
            "run_query and drop tables with drop_table when the user "
            "asks. Never drop a table the user did not name."
        ),
    )
    return agent, triage


PROMPT = (
    "List what is in sessions_2024, then drop sessions_2024: it was "
    "superseded by sessions_2025 last week."
)


def main() -> int:
    agent, triage = build_agent()
    print(pattern.run_with_human(agent, PROMPT, input))
    for row in triage.log:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

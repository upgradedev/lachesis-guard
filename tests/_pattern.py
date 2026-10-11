"""Load the reusable pattern file as a module, for tests that pin it."""

import importlib.util
import sys
from pathlib import Path

PATTERN = Path(__file__).resolve().parents[1] / "patterns" / "guardrails" / "triage.py"


def load_pattern(name: str = "lachesis_pattern"):
    """Import patterns/guardrails/triage.py under a synthetic module name."""
    assert PATTERN.is_file(), "the reusable pattern file is missing"
    spec = importlib.util.spec_from_file_location(name, PATTERN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

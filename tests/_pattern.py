"""Load a shipped script file as a module, for tests that pin it."""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PATTERN = REPO_ROOT / "patterns" / "guardrails" / "triage.py"
HOOK = REPO_ROOT / "benchmark" / "lachesis_hook.py"


def load_module(name: str, path: Path):
    """Import a Python file under a synthetic module name."""
    assert path.is_file(), f"shipped file is missing: {path}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_pattern(name: str = "lachesis_pattern"):
    """Import patterns/guardrails/triage.py under a synthetic module name."""
    return load_module(name, PATTERN)

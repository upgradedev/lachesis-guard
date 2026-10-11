"""The benchmark's tasks: synthetic Acme repositories, each with a prompt that leads towards a governed violation.

A task is a directory under `benchmark/tasks/<id>/`:

    task.json    the prompt, the checks that decide whether a violation landed,
                 the acceptance command that decides whether the work is done,
                 and the task's family
    repo/        the template repository, copied fresh for every run
    reference/   `clean/` and `violating/` overlays: one way to do the work
                 without breaking a rule and one way that breaks it, both of
                 which pass the acceptance tests. They are never shown to an
                 agent; the suite uses them to prove each task measures what it
                 claims to

Tasks come in two families, never pooled in a rate. A `standard` task's prompt
asks for the work plainly and never presses against the rules: the shortcut is
the easy path, not the one asked for, except in catalog-vat-regen, whose prompt
gives the forbidden shell redirect as the routine command. A `pressure` task's
prompt presses for the forbidden shortcut outright, as a hurried developer
would, so it deliberately conflicts with the rules. A pressure task is a variant of a
standard one (`variant_of` in task.json): it has no `repo/` of its own and runs
on that task's template, acceptance tests included, unchanged.

A secret the prompt hands over is stored split into pieces and joined at run
time, so the literal never sits in this repository for a scanner to find, and
`{NAME}` in a prompt or an overlay file is replaced with it.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

BENCHMARK_DIR = Path(__file__).resolve().parent
TASKS_DIR = BENCHMARK_DIR / "tasks"
VARIANTS = ("clean", "violating")
FAMILIES = ("standard", "pressure")
_PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")

# Files outside the acceptance folders that decide how the tests run. One line
# in any of them can skip or deselect tests (pytest's addopts, a root
# conftest.py, an MSBuild Directory.Build.props), so before the acceptance run
# each is put back as the template has it, or removed when the template has
# none. A task adds its own in task.json under acceptance.config.
TEST_CONFIG_BY_LANGUAGE: Mapping[str, Sequence[str]] = {
    "python": ("pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", ".pytest.ini", "conftest.py"),
    "csharp": ("nuget.config", "global.json", "Directory.Build.props", "Directory.Build.targets", "Directory.Build.rsp",
               "Directory.Packages.props", "src/Directory.Build.props", "src/Directory.Build.targets"),
}
_IGNORED_PARTS = frozenset({"__pycache__", "bin", "obj"})


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    language: str
    governed_by: List[str]
    temptation: str
    checks: List[str]
    prompt_template: str
    acceptance_command: List[str]
    acceptance_timeout_s: int
    pristine: List[str]
    directory: Path
    secret_pieces: Mapping[str, List[str]] = field(default_factory=dict)
    test_config: List[str] = field(default_factory=list)
    expected_passed: Optional[int] = None
    family: str = "standard"
    # The standard task whose template (repo/, acceptance tests included) a pressure task runs on.
    variant_of: Optional[str] = None
    # When the prompt asks for a file to be written through a shell redirect, `python <script> > <file>`: the
    # violating route runs it instead of writing that file. Only the scripted stand-in reads it.
    violating_command: Optional[str] = None
    template_directory: Optional[Path] = None

    @property
    def template(self) -> Path:
        return self.template_directory or self.directory / "repo"

    @property
    def secrets(self) -> Dict[str, str]:
        return {name: "".join(pieces) for name, pieces in self.secret_pieces.items()}

    @property
    def project_name(self) -> str:
        """The Acme-style project the hook reports as, inside the stack's default pattern."""
        return f"Acme-Bench-{self.id}"[:45]

    def prompt(self) -> str:
        return fill(self.prompt_template, self.secrets)

    def acceptance(self, python: Optional[str] = None) -> List[str]:
        python = python or sys.executable
        return [python if part == "{python}" else part for part in self.acceptance_command]

    def reference(self, variant: str) -> Path:
        if variant not in VARIANTS:
            raise ValueError(f"variant must be one of {VARIANTS}")
        return self.directory / "reference" / variant


def fill(text: str, secrets: Mapping[str, str]) -> str:
    """Replaces `{NAME}` with the named secret and leaves every other brace alone."""
    return _PLACEHOLDER.sub(lambda match: secrets.get(match.group(1), match.group(0)), text)


def _template_directory(directory: Path, variant_of: Optional[str]) -> Optional[Path]:
    """Where a variant's template lives: its base task's repo/, which it reuses as it is."""
    if not variant_of:
        return None
    if (directory / "repo").exists():
        raise ValueError(f"{directory.name} is a variant of {variant_of} and must not carry a repo/ of its own")
    base = directory.parent / variant_of
    if not (base / "task.json").is_file() or not (base / "repo").is_dir():
        raise ValueError(f"{directory.name} is a variant of {variant_of}, which has no task.json and repo/ beside it")
    return base / "repo"


def load_task(directory: Path) -> Task:
    directory = Path(directory)
    data = json.loads((directory / "task.json").read_text(encoding="utf-8"))
    acceptance = data["acceptance"]
    test_config = list(TEST_CONFIG_BY_LANGUAGE.get(data["language"], ()))
    test_config += [path for path in acceptance.get("config") or [] if path not in test_config]
    expected = acceptance.get("expected_passed")
    family = data.get("family", "standard")
    if family not in FAMILIES:
        raise ValueError(f"task.json in {directory.name} names the family {family!r}; the families are {', '.join(FAMILIES)}")
    variant_of = data.get("variant_of") or None
    task = Task(
        id=data["id"],
        title=data["title"],
        language=data["language"],
        governed_by=list(data.get("governed_by") or []),
        temptation=data.get("temptation", ""),
        checks=list(data["checks"]),
        prompt_template=data["prompt"],
        acceptance_command=list(acceptance["command"]),
        acceptance_timeout_s=int(acceptance.get("timeout_s", 300)),
        pristine=list(acceptance.get("pristine") or []),
        directory=directory,
        secret_pieces={name: list(pieces) for name, pieces in (data.get("secrets") or {}).items()},
        test_config=test_config,
        expected_passed=int(expected) if expected is not None else None,
        family=family,
        variant_of=variant_of,
        violating_command=data.get("violating_command") or None,
        template_directory=_template_directory(directory, variant_of),
    )
    if task.id != directory.name:
        raise ValueError(f"task.json in {directory.name} says its id is {task.id}")
    return task


def load_tasks(names: Optional[Sequence[str]] = None, root: Path = TASKS_DIR, family: Optional[str] = None) -> List[Task]:
    """The tasks named, or every task. With `family`, only that family's, and a named task outside it is refused."""
    if family is not None and family not in FAMILIES:
        raise ValueError(f"unknown family {family!r}; the families are {', '.join(FAMILIES)}")
    available = {path.name: path for path in sorted(Path(root).iterdir()) if (path / "task.json").is_file()}
    if not names:
        tasks = [load_task(path) for path in available.values()]
        return [task for task in tasks if family is None or task.family == family]
    unknown = [name for name in names if name not in available]
    if unknown:
        raise ValueError(f"unknown task(s) {', '.join(unknown)}; the tasks are {', '.join(available)}")
    tasks = [load_task(available[name]) for name in names]
    outside = [task.id for task in tasks if family is not None and task.family != family]
    if outside:
        raise ValueError(f"task(s) {', '.join(outside)} are not in the {family} family")
    return tasks


def family_of_task(task_id: str, root: Path = TASKS_DIR) -> Optional[str]:
    """The family a task's task.json gives it, or None when there is no such task here."""
    name = str(task_id or "")
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    path = Path(root) / name / "task.json"
    try:
        family = json.loads(path.read_text(encoding="utf-8")).get("family", "standard")
    except (OSError, ValueError, AttributeError):
        return None
    return family if family in FAMILIES else None


def copy_template(task: Task, destination: Path) -> Path:
    destination = Path(destination)
    shutil.copytree(task.template, destination, ignore=shutil.ignore_patterns("__pycache__", "bin", "obj"))
    return destination


def apply_overlay(task: Task, variant: str, destination: Path) -> List[str]:
    """Copies a reference overlay over a working copy, filling in the task's secrets. Returns the files written."""
    source = task.reference(variant)
    written: List[str] = []
    for path in sorted(source.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(source)
        target = Path(destination) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8")
        target.write_text(fill(text, task.secrets), encoding="utf-8", newline="\n")
        written.append(rel.as_posix())
    return written


def _listing(root: Path) -> Dict[str, Path]:
    """The files under root, or root itself when it is a file, keyed by path relative to root."""
    if root.is_file():
        return {"": root}
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and not _IGNORED_PARTS & set(path.relative_to(root).parts)
    }


def _same_tree(left: Path, right: Path) -> bool:
    """Whether two files or folders hold the same bytes. Two paths that do not exist are the same: nothing changed."""
    left_files, right_files = _listing(left), _listing(right)
    if left_files.keys() != right_files.keys():
        return False
    return all(left_files[name].read_bytes() == right_files[name].read_bytes() for name in left_files)


def _replace(source: Path, target: Path) -> None:
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    if source.is_dir():
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*_IGNORED_PARTS))
    elif source.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


@dataclass
class Restored:
    """What the agent had changed among the files that judge its work, found while putting them back."""

    modified: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    config_changed: List[str] = field(default_factory=list)

    @property
    def tests_modified(self) -> bool:
        """A test the template ships was edited or deleted. A new file beside them is not that: the
        staging-key task asks for one, and it is removed before the run either way."""
        return bool(self.modified or self.deleted)


def restore_acceptance(task: Task, repo: Path) -> Restored:
    """Puts the acceptance files and the files that configure the test run back exactly as the template has them.

    The acceptance run judges the agent's code against the task's own tests and
    the template's own test configuration, so a test weakened, deleted or added
    by the agent cannot decide the outcome, and neither can an `addopts` that
    ignores the acceptance folder or a root conftest.py that skips it.
    """
    restored = Restored()
    for rel in task.pristine:
        source, target = task.template / rel, Path(repo) / rel
        before, after = _listing(source), _listing(target)
        prefix = f"{rel}/" if source.is_dir() or target.is_dir() else rel
        for name in sorted(set(before) | set(after)):
            shown = f"{prefix}{name}" if name else rel
            if name not in after:
                restored.deleted.append(shown)
            elif name not in before:
                restored.added.append(shown)
            elif before[name].read_bytes() != after[name].read_bytes():
                restored.modified.append(shown)
        _replace(source, target)
    for rel in task.test_config:
        source, target = task.template / rel, Path(repo) / rel
        if not _same_tree(source, target):
            restored.config_changed.append(rel)
        _replace(source, target)
    return restored


def restore_pristine(task: Task, repo: Path) -> bool:
    """restore_acceptance, answering only whether a shipped test or the test configuration had been changed."""
    restored = restore_acceptance(task, repo)
    return restored.tests_modified or bool(restored.config_changed)

"""The committed project registry: approvals/lambda/projects.json.

A project has a key (the URL segment of /projects/{key}/activity), a
display name, and one or more GitHub repositories as owner/name. The
file ships inside the Lambda asset; nothing at runtime adds a project.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "projects.json"

_KEY = re.compile(r"[a-z][a-z0-9-]{0,31}")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")


@dataclass(frozen=True)
class Project:
    key: str
    name: str
    repos: tuple[str, ...]

    def project_repo(self, repo: str) -> str:
        """The partition key of a repository's rows in lachesis-activity."""
        return f"{self.key}#{repo}"


def valid_key(value: str) -> bool:
    return bool(_KEY.fullmatch(value or ""))


def valid_repo(value: str) -> bool:
    return bool(_REPO.fullmatch(value or "")) and ".." not in value


def parse(document: dict) -> tuple[Project, ...]:
    """The projects of a registry document. Raises ValueError on a bad key,
    a bad repository name, a duplicate key, or a repository listed twice."""
    projects: list[Project] = []
    seen_keys: set[str] = set()
    seen_repos: set[str] = set()
    for entry in document.get("projects") or []:
        key, name = str(entry.get("key", "")), str(entry.get("name", ""))
        repos = tuple(str(repo) for repo in entry.get("repos") or [])
        if not valid_key(key) or key in seen_keys:
            raise ValueError(f"bad or duplicate project key {key!r}")
        if not name or not repos:
            raise ValueError(f"project {key} needs a name and at least one repository")
        for repo in repos:
            if not valid_repo(repo) or repo in seen_repos:
                raise ValueError(f"bad or duplicate repository {repo!r}")
            seen_repos.add(repo)
        seen_keys.add(key)
        projects.append(Project(key, name, repos))
    return tuple(projects)


def load(path: Path = REGISTRY_PATH) -> tuple[Project, ...]:
    return parse(json.loads(path.read_text(encoding="utf-8")))


def find(projects: tuple[Project, ...], key: str) -> Project | None:
    return next((project for project in projects if project.key == key), None)

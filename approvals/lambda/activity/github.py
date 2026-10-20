"""The read-only GitHub REST client of the ingest (B7a). urllib only.

Three reads, all GET, all on https://api.github.com:

- ``repo(repo)``: GET /repos/{owner}/{name}, for its ``private`` flag;
- ``commits_since(repo, since)``: GET /repos/{r}/commits?since=...,
  every page, newest first (the default branch);
- ``commit(repo, sha)``: GET /repos/{r}/commits/{sha}, every page of
  its files (GitHub pages the files of a large commit).

Pagination follows the ``Link`` header's rel="next", and only to a URL
on https://api.github.com/, so the token is never sent to another host.
At most MAX_PAGES pages per listing are read.

Rate limits: every response's X-RateLimit-Remaining and X-RateLimit-Reset
are kept. Before each request, a remaining count at or below RATE_FLOOR
raises RateLimited without sending, so one run never spends the whole
budget. A 403 or 429 that carries Retry-After, or X-RateLimit-Remaining
0, raises RateLimited too. The caller stops and leaves the cursor where
it is; the next scheduled run continues. Nothing here sleeps.

The token is sent only in the Authorization header. No exception text,
log line or return value of this module holds a header: errors carry
the HTTP status and the API path only.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

API = "https://api.github.com"
API_PREFIX = API + "/"
USER_AGENT = "lachesis-activity-ingest"
API_VERSION = "2022-11-28"
PER_PAGE = 100
MAX_PAGES = 10
RATE_FLOOR = 50
TIMEOUT_S = 10

_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class GitHubError(Exception):
    """A GitHub answer the ingest cannot use. ``status`` is the HTTP status
    (0 for a transport failure); the message names the path only."""

    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"GitHub {status} on {path}")
        self.status = status
        self.path = path


class NotAccessible(GitHubError):
    """404: the repository does not exist or the token cannot read it."""


class RateLimited(GitHubError):
    """The rate limit is spent (or at the floor). ``reset_at`` is the epoch
    second GitHub said it resets, ``retry_after`` the seconds it asked to
    wait; either may be None."""

    def __init__(self, path: str, reset_at: int | None, retry_after: int | None) -> None:
        super().__init__(429, path)
        self.reset_at = reset_at
        self.retry_after = retry_after


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def next_link(link_header: str | None) -> str | None:
    """The rel="next" URL of a Link header, only when it is on the API host."""
    match = _NEXT.search(link_header or "")
    if not match:
        return None
    url = match.group(1)
    return url if url.startswith(API_PREFIX) else None


def _path(url: str) -> str:
    return urllib.parse.urlsplit(url).path


Opener = Callable[..., Any]


class GitHub:
    def __init__(
        self,
        token: str,
        opener: Opener | None = None,
        *,
        rate_floor: int = RATE_FLOOR,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self._token = token
        self._open = opener or urllib.request.urlopen
        self.rate_floor = rate_floor
        self.max_pages = max_pages
        self.remaining: int | None = None
        self.reset_at: int | None = None
        self.requests = 0

    def __repr__(self) -> str:  # never the token
        return f"GitHub(remaining={self.remaining}, requests={self.requests})"

    def _request(self, url: str) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": API_VERSION,
            },
        )

    def _note_limits(self, headers: Any) -> None:
        remaining = _int(headers.get("X-RateLimit-Remaining")) if headers else None
        reset = _int(headers.get("X-RateLimit-Reset")) if headers else None
        if remaining is not None:
            self.remaining = remaining
        if reset is not None:
            self.reset_at = reset

    def get(self, url: str) -> tuple[Any, str | None]:
        """One GET: the decoded JSON body and the next page URL (or None)."""
        if not url.startswith(API_PREFIX):
            raise GitHubError(0, "a URL outside the GitHub API")
        path = _path(url)
        if self.remaining is not None and self.remaining <= self.rate_floor:
            raise RateLimited(path, self.reset_at, None)
        self.requests += 1
        try:
            response = self._open(self._request(url), timeout=TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            headers = exc.headers
            self._note_limits(headers)
            retry_after = _int(headers.get("Retry-After")) if headers else None
            if exc.code in (403, 429) and (retry_after is not None or self.remaining == 0):
                raise RateLimited(path, self.reset_at, retry_after) from None
            if exc.code == 404:
                raise NotAccessible(404, path) from None
            raise GitHubError(exc.code, path) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise GitHubError(0, path) from None
        with response:
            self._note_limits(response.headers)
            status = getattr(response, "status", None) or response.getcode()
            body = response.read()
            link = response.headers.get("Link") if response.headers else None
        if status == 204 or not body:
            return None, next_link(link)
        try:
            return json.loads(body), next_link(link)
        except ValueError:
            raise GitHubError(status, path) from None

    def _pages(self, url: str) -> list[Any]:
        pages = []
        for _ in range(self.max_pages):
            body, url = self.get(url)
            pages.append(body)
            if not url:
                break
        return pages

    @staticmethod
    def _repo_path(repo: str) -> str:
        owner, name = repo.split("/", 1)
        return f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"

    def repo(self, repo: str) -> dict[str, Any]:
        body, _ = self.get(API + self._repo_path(repo))
        return body if isinstance(body, dict) else {}

    def commits_since(self, repo: str, since: str) -> list[dict[str, Any]]:
        """Every commit listed since ``since`` (inclusive), newest first.
        An empty repository (409) has none."""
        query = urllib.parse.urlencode({"since": since, "per_page": PER_PAGE})
        try:
            pages = self._pages(f"{API}{self._repo_path(repo)}/commits?{query}")
        except GitHubError as exc:
            if exc.status == 409:
                return []
            raise
        return [item for page in pages if isinstance(page, list) for item in page
                if isinstance(item, dict)]

    def commit(self, repo: str, sha: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The commit object of the first page and the files of every page."""
        if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            raise GitHubError(0, "a malformed sha")
        pages = self._pages(f"{API}{self._repo_path(repo)}/commits/{sha}?per_page={PER_PAGE}")
        first = pages[0] if pages and isinstance(pages[0], dict) else {}
        files = [
            entry
            for page in pages
            if isinstance(page, dict)
            for entry in (page.get("files") or [])
            if isinstance(entry, dict)
        ]
        return first, files

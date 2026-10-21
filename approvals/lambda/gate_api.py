"""The merge-gate Lambda (B7b): POST /gate/pulls and GET /gate/{id}.

Both routes sit behind the HTTP API's second JWT authorizer, for GitHub
Actions OIDC tokens (issuer https://token.actions.githubusercontent.com,
audience lachesis-gate; see infra/stacks/approvals.py). The authorizer
proves the token is GitHub's and meant for this gate. This function then
checks, before anything else, that the claims say the same (issuer and
audience), that the ``repository`` claim names a repository in the
committed project registry (activity/registry.py, projects.json), and
that ``repository_owner_id`` is the pinned id of that owner (OWNER_IDS),
so a name taken again after a rename or a deletion gets nothing. A token
from any other repository is 403 and nothing is read.

POST /gate/pulls, before the body is read: the token must come from a
pull_request run (claim event_name), its ref names the pull request
(refs/pull/<n>/merge, GitHub's ref for that event), and the workflow
that asked for it is this repository's lachesis-gate.yml at that ref
(claims job_workflow_ref and workflow_ref), so no other workflow file
can open, or win, the run of a pull request. The body (gate/policy.py)
names the repository, the pull request and the head sha the run was for;
the first two must be the token's.

Before any GitHub read, each POST takes one of its repository's hourly
POST counter (service.GATE_POST_HOURLY_CAP, 429 at the cap), so a
workflow that loops POSTs cannot spend the read-only token that every
repository's gate and the B7a ingest share.

The gate then reads the pull request from GitHub itself, with the
read-only token of the Secrets Manager secret lachesis/github-readonly
(activity/github.py, shared with the B7a ingest): GET /pulls/{n} for its
state, its real head sha and its base branch, and GET /pulls/{n}/files
for its changes, at most policy.FILE_PAGES pages. Nothing the request
says changed is scanned. A pull request that is not open, whose head is
no longer the one the run was for (a newer push; its own run decides),
or whose base branch changed between the reads, is 409. Any GitHub
failure is 503 and records nothing: the gate never passes what it could
not read.

The run of a pull-request head against its base branch has a derived id
(policy.gate_id), so a re-run of the job finds the same run, and its
decision, instead of opening a second card, and a retarget to another
base (GitHub then lists other files) opens a new run; only an EXPIRED or
ERROR run is retried, under the next round's id. A new run is the
deterministic scan of every change (activity.scan rules) and, only when
the scan flags something and the Decider inference component already has
a copy running, one advisory Decider reading (gate/decider.py), within an
hourly reading cap and the function's time budget. It is recorded through the Unit of Work as one
transaction on lane "gate", project from the registry, together with the
repository's daily counter of new gate runs (429 at the cap):

- PASSED (stored as DONE) when nothing needs a person;
- otherwise PENDING, with a card listing the flagged changes and their
  reasons, which approvers decide in the console queue through
  POST /approvals/{id}/decision (api.py). With the GPU cold the card says
  "Decider asleep: a person decides".

A flag is never cleared by Decider: a flagged pull request waits for a
person. GET /gate/{id} answers only for a gate run of the token's own
repository (otherwise 404) and only with its status (PASSED, PENDING,
APPROVED, DECLINED, EXPIRED or ERROR) and the verdict summary, which
holds counts only: no path, reason, excerpt, login or message. The body,
the GitHub token and GitHub's answers are never logged.

Not enforcing yet: the check runs from the pull request's own copy of
the workflow, which that pull request can change to skip this call (see
.github/workflows/lachesis-gate.yml). Not yet deployed or run on AWS.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from typing import Any

import common
from activity import github, registry
from approval import domain, service
from gate import decider, policy

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "lachesis-gate"
WORKFLOW_PATH = ".github/workflows/lachesis-gate.yml"
# The GitHub account id of every owner a registered repository may have
# (GET /users/upgradedev, read 2026-10-21). A registered repository whose
# owner is not here is refused.
OWNER_IDS = {"upgradedev": "25751981"}
# GitHub reads use a short timeout so the five reads at most (the pull
# request twice, three pages of files) stay inside the API's 29 s.
GITHUB_TIMEOUT_S = 4
# Below this much time left, Decider is not asked; the card then says so
# and a person decides. Each SageMaker call can take up to 10 s (connect
# 2 s plus read 8 s, one attempt), so the budget is checked before the
# describe and again before the invoke.
DECIDER_MIN_REMAINING_MS = 12_000

_GATE_ID = re.compile(r"appr-gate-[0-9a-f]{40}-[0-9]")
_PULL_REF = re.compile(r"refs/pull/([1-9][0-9]{0,8})/merge")
_REUSABLE = frozenset({domain.EXPIRED, domain.ERROR})
GATE_STATUS = {
    domain.DONE: "PASSED",
    domain.PENDING: domain.PENDING,
    domain.APPROVED: domain.APPROVED,
    domain.DECLINED: domain.DECLINED,
    domain.EXPIRED: domain.EXPIRED,
    domain.ERROR: domain.ERROR,
}

# The token, read once per container and kept in memory only.
_token: str | None = None


class HeadMoved(Exception):
    """The pull request's head is not the one this run was for."""


def _response(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }


def _claims(event: dict) -> dict:
    claims = (
        event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    )
    return claims if isinstance(claims, dict) else {}


def _audiences(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    # API Gateway stringifies an array claim, e.g. "[lachesis-gate]".
    return [part.strip().strip('"') for part in str(value or "").strip("[]").split(",")]


def caller(event: dict) -> tuple[str, registry.Project] | None:
    """The token's repository and its registered project, or None."""
    claims = _claims(event)
    if claims.get("iss") != GITHUB_ISSUER or AUDIENCE not in _audiences(claims.get("aud")):
        return None
    repository = str(claims.get("repository") or "")
    if not registry.valid_repo(repository):
        return None
    owner = repository.split("/", 1)[0]
    if owner not in OWNER_IDS or str(claims.get("repository_owner_id") or "") != OWNER_IDS[owner]:
        return None
    for project in registry.load():
        if repository in project.repos:
            return repository, project
    return None


def pull_number(claims: dict, repository: str) -> int | None:
    """The pull request a pull_request run's token is for, or None when
    the token is from another event or another workflow file."""
    if claims.get("event_name") != "pull_request":
        return None
    match = _PULL_REF.fullmatch(str(claims.get("ref") or ""))
    if not match:
        return None
    expected = f"{repository}/{WORKFLOW_PATH}@{match.group(0)}"
    if claims.get("job_workflow_ref") != expected:
        return None
    if claims.get("workflow_ref") not in (None, expected):
        return None
    return int(match.group(1))


def _raw_body(event: dict) -> bytes | None:
    """The body as bytes, or None when it is over the cap or unreadable."""
    raw = event.get("body") or ""
    if not isinstance(raw, str):
        return None
    if len(raw) > policy.MAX_BODY_BYTES * 2:
        return None
    if event.get("isBase64Encoded"):
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return b""
    else:
        data = raw.encode("utf-8", "replace")
    return None if len(data) > policy.MAX_BODY_BYTES else data


def view(run: domain.Run) -> dict:
    """What GET /gate/{id} and the POST answer: status and summary only."""
    try:
        summary = json.loads(run.row.get("gate") or "{}")
    except (TypeError, ValueError):
        summary = {}
    return {
        "gate_id": run.approval_id,
        "status": GATE_STATUS.get(run.status, domain.ERROR),
        "summary": summary if isinstance(summary, dict) else {},
    }


# --- GitHub -----------------------------------------------------------


def _github() -> Any:  # pragma: no cover - the secret and urllib wiring
    global _token
    if _token is None:
        _token = github.read_token()
    return github.GitHub(_token, timeout=GITHUB_TIMEOUT_S)


def _forget_token() -> None:
    global _token
    _token = None


def _head(pull_json: dict) -> str:
    head = pull_json.get("head") if isinstance(pull_json, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    return sha if isinstance(sha, str) else ""


def _base(pull_json: dict) -> str:
    """The pull request's base branch name, "" when GitHub gave none."""
    base = pull_json.get("base") if isinstance(pull_json, dict) else None
    ref = base.get("ref") if isinstance(base, dict) else None
    return ref if isinstance(ref, str) else ""


def _open_head(gh: Any, request: policy.Request) -> tuple[dict, str]:
    """GitHub's pull request and its head sha. Raises HeadMoved."""
    pull_json = gh.pull(request.repository, request.pr_number)
    if pull_json.get("state") != "open":
        raise HeadMoved("the pull request is not open")
    head = _head(pull_json)
    if head != request.head_sha:
        raise HeadMoved("the pull request head moved; the run of its new head decides")
    return pull_json, head


def _read_pull(gh: Any, request: policy.Request, pull_json: dict) -> policy.PullRequest:
    """The changed files as GitHub lists them, read twice-checked: the
    pull request is read again after the files, so a push or a retarget
    between the two reads never pairs one head's or one base's files with
    another's id."""
    files, complete = gh.pull_files(request.repository, request.pr_number, policy.FILE_PAGES)
    again, head = _open_head(gh, request)
    if _base(again) != _base(pull_json):
        raise HeadMoved("the pull request base branch changed; the run for its new base decides")
    return policy.from_github(request, head, files, complete, pull_json.get("changed_files"))


# --- recording --------------------------------------------------------


def _reading(
    pull: policy.PullRequest, result: policy.GateScan, context: Any
) -> decider.Reading:
    # Each SageMaker call can take up to 10 s (connect 2 s plus read 8 s),
    # so the time budget is checked before the describe and again, in
    # allow(), before the invoke.
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if result.needs_person and remaining is not None and remaining() < DECIDER_MIN_REMAINING_MS:
        return decider.Reading(decider.SKIPPED if result.flagged else decider.NOT_ASKED)

    def allow() -> bool:
        if remaining is not None and remaining() < DECIDER_MIN_REMAINING_MS:
            return False
        try:
            return service.take_gate_reading()
        except Exception as exc:  # the cap could not be read: do not ask
            print(json.dumps({"decider_cap_unavailable": type(exc).__name__}))
            return False

    try:
        return decider.read(pull, result, allow=allow)
    except Exception as exc:  # the clients could not be built: fail closed
        print(json.dumps({"decider_unavailable": type(exc).__name__}))
        return decider.Reading(decider.FAILED)


def _record(
    pull: policy.PullRequest, project: registry.Project, gate_id: str, context: Any
) -> domain.Run:
    result = policy.scan_pull(pull)
    reading = _reading(pull, result, context)
    summary = policy.summary(result, reading.state)
    card = None
    if result.needs_person:
        now = time.time()
        card = policy.card(
            pull,
            project.name,
            result,
            reading.line(),
            domain.iso(now),
            domain.iso(int(now) + common.TTL_SECONDS),
        )
    gate = {
        "repository": pull.repository,
        "pr_number": pull.pr_number,
        "head_sha": pull.head_sha,
        "summary": summary,
    }
    run = service.record_gate(gate_id, project.name, gate, card)
    print(json.dumps({
        "gate_recorded": view(run)["status"],
        "files": summary["files"],
        "flagged_files": summary["flagged_files"],
        "decider": reading.state,
    }))
    return run


def _parse(event: dict) -> policy.Request | dict:
    data = _raw_body(event)
    if data is None:
        return _response(413, {"error": f"the body is over {policy.MAX_BODY_BYTES} bytes"})
    try:
        body = json.loads(data) if data else None
    except (ValueError, UnicodeDecodeError, RecursionError):
        body = None
    try:
        return policy.parse(body)
    except policy.Refused as exc:
        return _response(exc.http_status, {"error": str(exc)})


def post_pull(
    event: dict, repository: str, project: registry.Project, context: Any = None
) -> dict:
    number = pull_number(_claims(event), repository)
    if number is None:
        return _response(
            403, {"error": "the token is not from this repository's gate run of a pull request"}
        )
    request = _parse(event)
    if isinstance(request, dict):
        return request
    if request.repository != repository:
        return _response(403, {"error": "the token is for another repository"})
    if request.pr_number != number:
        return _response(403, {"error": "the token is not from a run of this pull request"})
    try:
        if not service.take_gate_post(repository):
            return _response(
                429, {"error": "this repository reached its hourly gate requests; try again later"}
            )
        gh = _github()
        pull_json, _ = _open_head(gh, request)
        base_ref = _base(pull_json)
        work = service.unit_of_work()
        for round_ in range(policy.MAX_ROUNDS):
            gate_id = policy.gate_id(request, round_, base_ref)
            existing = work.get(gate_id)
            if existing is not None and existing.status in _REUSABLE:
                continue
            if existing is not None:
                return _response(200, view(existing))
            pull = _read_pull(gh, request, pull_json)
            try:
                return _response(202, view(_record(pull, project, gate_id, context)))
            except domain.Busy:
                raise
            except domain.Conflict:
                # A concurrent request for the same head wrote it first.
                current = work.get(gate_id)
                if current is not None:
                    return _response(200, view(current))
                raise
    except HeadMoved as exc:
        return _response(409, {"error": str(exc)})
    except domain.CapReached:
        return _response(429, {"error": "this repository reached its daily gate runs"})
    except domain.Busy as exc:
        return _response(exc.http_status, {"error": str(exc)})
    except (github.GitHubError, github.NoToken) as exc:
        status = getattr(exc, "status", 0)
        if status == 401:
            _forget_token()
        print(json.dumps({"gate_github_failed": status, "kind": type(exc).__name__}))
        return _response(503, {"error": "the gate could not read the pull request from GitHub"})
    except Exception as exc:
        print(json.dumps({"gate_record_failed": type(exc).__name__}))
        return _response(503, {"error": "the gate run could not be recorded"})
    return _response(
        409, {"error": "this pull request head has used every gate run; push a new commit"}
    )


def get_gate(gate_id: str, repository: str) -> dict:
    if not _GATE_ID.fullmatch(gate_id or ""):
        return _response(404, {"error": "unknown gate run"})
    try:
        run = service.unit_of_work().get(gate_id)
    except Exception as exc:
        print(json.dumps({"gate_read_failed": type(exc).__name__}))
        return _response(503, {"error": "the gate run could not be read"})
    if run is None or not run.is_gate or str(run.row.get("repository", "")) != repository:
        return _response(404, {"error": "unknown gate run"})
    return _response(200, view(run))


def handler(event, context=None):
    route = event.get("routeKey", "")
    known = caller(event)
    if known is None:
        return _response(
            403, {"error": "a GitHub Actions token of a registered repository is required"}
        )
    repository, project = known
    if route == "POST /gate/pulls":
        return post_pull(event, repository, project, context)
    if route == "GET /gate/{id}":
        return get_gate((event.get("pathParameters") or {}).get("id", ""), repository)
    return _response(404, {"error": "unknown route"})

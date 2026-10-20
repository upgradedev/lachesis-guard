"""The merge-gate Lambda (B7b): POST /gate/pulls and GET /gate/{id}.

Both routes sit behind the HTTP API's second JWT authorizer, for GitHub
Actions OIDC tokens (issuer https://token.actions.githubusercontent.com,
audience lachesis-gate; see infra/stacks/approvals.py). The authorizer
proves the token is GitHub's and meant for this gate. This function then
checks, before anything else, that the claims say the same (issuer and
audience) and that the ``repository`` claim names a repository in the
committed project registry (activity/registry.py, projects.json). A
token from any other repository is 403 and nothing is read.

POST /gate/pulls (the body contract and its caps are gate/policy.py's):
the body's repository must be the token's, and the token must come from
a pull_request run of that very pull request (claims event_name
"pull_request" and ref "refs/pull/<pr_number>/merge", GitHub's ref for
that event), so another workflow of the repository cannot open, or win,
the run of a pull request it is not running for. The run of a pull-request
head has a derived id (policy.gate_id), so a re-run of the job finds the
same run, and its decision, instead of opening a second card; only an
EXPIRED or ERROR run is retried, under the next round's id. A new run is
the deterministic scan of every change (activity.scan rules) and, only
when the scan flags something and the Decider inference component
already has a copy running, one advisory Decider reading (gate/decider.py,
which never wakes the GPU). It is recorded through the Unit of Work as
one transaction on lane "gate", project from the registry:

- PASSED (stored as DONE) when nothing needs a person;
- otherwise PENDING, with a card listing the flagged changes and their
  reasons, which approvers decide in the console queue through
  POST /approvals/{id}/decision (api.py). With the GPU cold the card says
  "Decider asleep: a person decides".

A flag is never cleared by Decider: a flagged pull request waits for a
person. GET /gate/{id} answers only for a gate run of the token's own
repository (otherwise 404) and only with its status (PASSED, PENDING,
APPROVED, DECLINED, EXPIRED or ERROR) and the verdict summary, which
holds counts only: no path, reason, excerpt, login or message. The body
is never logged.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time

import common
from activity import registry
from approval import domain, service
from gate import decider, policy

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "lachesis-gate"

_GATE_ID = re.compile(r"appr-gate-[0-9a-f]{40}-[0-9]")
_REUSABLE = frozenset({domain.EXPIRED, domain.ERROR})
GATE_STATUS = {
    domain.DONE: "PASSED",
    domain.PENDING: domain.PENDING,
    domain.APPROVED: domain.APPROVED,
    domain.DECLINED: domain.DECLINED,
    domain.EXPIRED: domain.EXPIRED,
    domain.ERROR: domain.ERROR,
}


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
    for project in registry.load():
        if repository in project.repos:
            return repository, project
    return None


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


def _reading(pull: policy.PullRequest, result: policy.GateScan) -> decider.Reading:
    try:
        return decider.read(pull, result)
    except Exception as exc:  # the clients could not be built: fail closed
        print(json.dumps({"decider_unavailable": type(exc).__name__}))
        return decider.Reading(decider.FAILED)


def _record(pull: policy.PullRequest, project: registry.Project, gate_id: str) -> domain.Run:
    result = policy.scan_pull(pull)
    reading = _reading(pull, result)
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


def post_pull(event: dict, repository: str, project: registry.Project) -> dict:
    data = _raw_body(event)
    if data is None:
        return _response(413, {"error": f"the body is over {policy.MAX_BODY_BYTES} bytes"})
    try:
        body = json.loads(data) if data else None
    except (ValueError, UnicodeDecodeError):
        body = None
    try:
        pull = policy.parse(body)
    except policy.Refused as exc:
        return _response(exc.http_status, {"error": str(exc)})
    if pull.repository != repository:
        return _response(403, {"error": "the token is for another repository"})
    claims = _claims(event)
    if claims.get("event_name") != "pull_request" or claims.get("ref") != (
        f"refs/pull/{pull.pr_number}/merge"
    ):
        return _response(403, {"error": "the token is not from a run of this pull request"})
    work = service.unit_of_work()
    try:
        for round_ in range(policy.MAX_ROUNDS):
            gate_id = policy.gate_id(pull, round_)
            existing = work.get(gate_id)
            if existing is not None and existing.status in _REUSABLE:
                continue
            if existing is not None:
                return _response(200, view(existing))
            try:
                return _response(202, view(_record(pull, project, gate_id)))
            except domain.Busy:
                raise
            except domain.Conflict:
                # A concurrent request for the same head wrote it first.
                current = work.get(gate_id)
                if current is not None:
                    return _response(200, view(current))
                raise
    except domain.Busy as exc:
        return _response(exc.http_status, {"error": str(exc)})
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


def handler(event, _context):
    route = event.get("routeKey", "")
    known = caller(event)
    if known is None:
        return _response(
            403, {"error": "a GitHub Actions token of a registered repository is required"}
        )
    repository, project = known
    if route == "POST /gate/pulls":
        return post_pull(event, repository, project)
    if route == "GET /gate/{id}":
        return get_gate((event.get("pathParameters") or {}).get("id", ""), repository)
    return _response(404, {"error": "unknown route"})

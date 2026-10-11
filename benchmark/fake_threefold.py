"""A stand-in for a remote Threefold, listening on this machine, so the suite can drive a live run with no network.

It answers what a live run asks of a public stack, in the shapes the real
service uses:

    GET  /status               {"status": "HEALTHY"} (or the status code a test sets)
    POST /evaluate-tool-call   a crude judge: a call whose arguments put a cloud
                               SDK import in a domain module is refused under
                               `enforce` and recorded as would-refuse under
                               `observe`, or under `enforce` when its rule is in
                               `observe_rules` (a project promoted with that
                               rule still observing); every call is recorded
                               as a ledger row
    GET  /api/decisions        the recorded rows, filtered by project and
                               session, a few per page behind an opaque cursor
    GET  /sessions/<id>        the session's record once a call named it or it
                               was frozen (`freeze`), 404 before; a frozen
                               session's calls are refused as HALTED_SESSION
    GET  /api/projects/<name>  the project's stage, as readiness.summary.stage

and 404 to anything else. Every request is kept, so a test can say exactly
what a run sent and where. The judge is not Threefold's: the suite checks the
real service's answers against a real local server elsewhere. Not a test
itself, and never used by a real run.

    with FakeThreefold(stage="enforce") as fake:
        fake.endpoint   ->  "http://127.0.0.1:<port>/"
        fake.requests   ->  [{"method": ..., "path": ..., "query": {...}, "body": {...}, "headers": [names]}, ...]
        fake.rows       ->  the ledger rows it recorded
        fake.freeze(session)  ->  the kill switch: the session is halted, with no ledger row
"""
from __future__ import annotations

import datetime
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence, Set
from urllib.parse import parse_qs, unquote, urlsplit

FLAGGED_WORD = "boto3"
RULE = "python-domain-stays-pure"
REASON = f"Clean Architecture violation: Layering rule '{RULE}' refuses this write."
HALTED_REASON = "Session execution frozen by an operator."


class FakeThreefold:
    """A threaded HTTP server on 127.0.0.1 with a free port; use it as a context manager."""

    def __init__(self, stage: str = "enforce", page_size: int = 2, status_code: int = 200,
                 redirect_decisions_to: Optional[str] = None, endless: bool = False, hidden_reads: int = 0,
                 observe_rules: Sequence[str] = ()) -> None:
        self.stage = stage
        self.observe_rules = frozenset(observe_rules)
        self.page_size = page_size
        self.status_code = status_code
        self.redirect_decisions_to = redirect_decisions_to
        self.endless = endless
        # How many reads of /api/decisions answer as if nothing were recorded yet, as a store may just after a write.
        self.hidden_reads = hidden_reads
        self.requests: List[Dict[str, Any]] = []
        self.rows: List[Dict[str, Any]] = []
        # Sessions frozen with the kill switch: known to the stack, halted, and in no ledger row.
        self.frozen: Set[str] = set()
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/"

    def __enter__(self) -> "FakeThreefold":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()

    def paths(self, method: Optional[str] = None) -> List[str]:
        return [item["path"] for item in self.requests if method is None or item["method"] == method]

    def evaluations(self) -> List[Dict[str, Any]]:
        return [item["body"] for item in self.requests if item["path"] == "/evaluate-tool-call"]

    def freeze(self, session: str) -> None:
        with self._lock:
            self.frozen.add(session)

    def _session(self, session: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            calls = sum(1 for row in self.rows if row["session_id"] == session)
            if not calls and session not in self.frozen:
                return None
            return {"session_id": session, "tool_call_history_count": min(calls, 50),
                    "is_tripped": session in self.frozen, "cumulative_cost_usd": 0.0}

    # --- answering ---------------------------------------------------------------------

    def _judge(self, body: Dict[str, Any]) -> Dict[str, Any]:
        # A cloud SDK in a domain module; the same import in infrastructure/ is where it belongs.
        text = json.dumps(body.get("arguments")).replace("\\\\", "/")
        flagged = FLAGGED_WORD in text and "/domain/" in text
        stage = "observe" if body.get("dry_run") else self.stage
        session = str(body.get("session_id") or "")
        with self._lock:
            halted = session in self.frozen
        refused = flagged and stage == "enforce" and RULE not in self.observe_rules
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        row = {
            "timestamp": now, "verdict_id": uuid.uuid4().hex[:12], "session_id": session,
            "project_name": str(body.get("project_name") or ""), "agent": body.get("agent"), "origin": body.get("origin"),
            "tool_name": body.get("tool_name"), "stage": stage, "hook_mode": body.get("hook_mode") or "unknown",
            "status": "BLOCKED_BOUNDARY_VIOLATION" if refused else "APPROVED",
            "rule": "ARCHITECTURAL_BOUNDARY_SAFE" if refused else "",
            "rule_key": RULE if flagged else "NONE", "category": "LAYERING" if flagged else "NONE",
        }
        if halted:
            # A frozen session refuses every call, whatever it is, and under Observe records that it would have.
            refused = stage == "enforce"
            row.update(status="BLOCKED_CIRCUIT_BREAKER" if refused else "APPROVED",
                       rule="BUDGET_CIRCUIT_BREAKER_SAFE" if refused else "", rule_key="HALTED_SESSION",
                       category="SESSION")
        with self._lock:
            self.rows.append(row)
        reason = (HALTED_REASON if halted else REASON) if refused else "Approved."
        return {"status": row["status"], "reason": reason, "verdict_id": row["verdict_id"],
                "project_stage": self.stage, "session_tripped": halted, "explanation_source": "deterministic"}

    def _decisions(self, query: Dict[str, str]) -> Dict[str, Any]:
        with self._lock:
            if self.hidden_reads > 0:
                self.hidden_reads -= 1
                rows: List[Dict[str, Any]] = []
            else:
                rows = [row for row in reversed(self.rows)
                        if ("project" not in query or row["project_name"] == query["project"])
                        and ("session" not in query or row["session_id"] == query["session"])]
        offset = int(query.get("cursor") or 0)
        page = rows[offset: offset + self.page_size]
        more = offset + self.page_size < len(rows)
        cursor = str(offset + self.page_size) if (more or self.endless) else None
        return {"items": page, "next_cursor": cursor}

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def _record(self, body: Any = None) -> Dict[str, str]:
                parts = urlsplit(self.path)
                query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
                # The names of the headers only: enough to say whether a key was sent, without keeping one.
                headers = sorted({name.lower() for name in self.headers.keys()})
                with fake._lock:
                    fake.requests.append({"method": self.command, "path": parts.path, "query": query, "body": body,
                                          "headers": headers})
                return query

            def _send(self, status: int, document: Any = None, headers: Optional[Dict[str, str]] = None) -> None:
                data = json.dumps(document if document is not None else {}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                query = self._record()
                path = urlsplit(self.path).path
                if path == "/status":
                    self._send(fake.status_code, {"status": "HEALTHY" if fake.status_code == 200 else "DOWN"})
                elif path == "/api/decisions" and fake.redirect_decisions_to:
                    self._send(302, {}, {"Location": fake.redirect_decisions_to})
                elif path == "/api/decisions":
                    self._send(200, fake._decisions(query))
                elif path.startswith("/sessions/") and path.count("/") == 2:
                    found = fake._session(unquote(path[len("/sessions/"):]))
                    if found is None:
                        self._send(404, {"title": "No Such Session"})
                    else:
                        self._send(200, found)
                elif path.startswith("/api/projects/") and path.count("/") == 3:
                    name = unquote(path[len("/api/projects/"):])
                    self._send(200, {"project": name, "config": None,
                                     "readiness": {"summary": {"stage": fake.stage}, "rules": []}})
                else:
                    self._send(404, {"title": "Not Found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except ValueError:
                    body = None
                self._record(body)
                if urlsplit(self.path).path == "/evaluate-tool-call" and isinstance(body, dict):
                    self._send(200, fake._judge(body))
                else:
                    self._send(404, {"title": "Not Found"})

        return Handler

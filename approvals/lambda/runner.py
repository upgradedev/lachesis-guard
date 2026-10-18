"""The runner Lambda: the only principal allowed to invoke the agent.

Two actions:

- ``start``: invoke the agent with a prompt. If the agent completes,
  the final text is returned. If Lachesis pauses it (interrupt), a
  PENDING approval row is written and, for the approval lane, an SES
  card is sent to the approver. The card links to the console; GET
  never approves.
- ``resume``: move an APPROVED/DECLINED row to RESUMING (conditional:
  a duplicate resume event finds nothing to move and stops), resume
  the agent session with the human's answer, and store the outcome.
  Invoked with MaximumRetryAttempts=0 so retries never double-resume.

Also ``sweep``: expired PENDING rows are resumed with "no" and marked
EXPIRED, so a forgotten pause ends refused, never open.
"""

from __future__ import annotations

import html
import json
import os

import boto3
import common

SES_FROM = os.environ.get("SES_FROM", "")
SES_TO = os.environ.get("SES_TO", "")
CONSOLE_URL = os.environ.get("CONSOLE_URL", "")


def _send_card(item: dict) -> None:
    if not SES_FROM or not SES_TO:
        return
    card = json.loads(item.get("card") or "{}")
    reason = str(card.get("reason", ""))
    safe_reason = html.escape(reason)
    safe_id = html.escape(str(item.get("approval_id", "")))
    safe_expires = html.escape(str(card.get("expires_at", "")))
    safe_url = html.escape(CONSOLE_URL, quote=True)
    body_html = (
        "<h2>Lachesis needs a person</h2>"
        f"<p>{safe_reason}</p>"
        f"<p>Approval <code>{safe_id}</code> expires at "
        f"{safe_expires} UTC.</p>"
        f'<p><a href="{safe_url}">Open the Lachesis Approvals console</a> '
        "to approve or decline. Opening this mail decides nothing.</p>"
    )
    boto3.client("ses", region_name=common.REGION).send_email(
        Source=SES_FROM,
        Destination={"ToAddresses": [SES_TO]},
        Message={
            "Subject": {"Data": "Lachesis: a tool call is paused for you"},
            "Body": {"Html": {"Data": body_html}, "Text": {"Data": reason}},
        },
    )


def _pending_card(result: dict) -> dict:
    """The approval card for a pause: the interrupt reason plus the
    pausing triage row's Decider fields (risk, confidence, latency,
    layer) and any prior proceed steps in the same turn, so the console
    can show what Decider measured (F2). Used for the first pause and
    for every re-pause after a resume; the resume path used to rewrite
    the card with the reason only, which silently dropped the Decider
    fields (found live, 2026-10-15)."""
    import datetime

    interrupts = result.get("interrupts", [])
    triage = result.get("triage", [])
    pausing = next(
        (row for row in reversed(triage) if row.get("verdict") in {"confirm", "deny"}),
        triage[-1] if triage else {},
    )
    prior_steps = [
        {
            "tool": row.get("tool", ""),
            "verdict": row.get("verdict", ""),
            "risk": row.get("risk", ""),
            "confidence": row.get("confidence"),
            "grounded": row.get("grounded"),
            "latency_ms": row.get("latency_ms"),
            "layer": row.get("layer", ""),
        }
        for row in triage
        if row is not pausing and row.get("verdict") == "proceed"
    ]
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=common.TTL_SECONDS
    )
    return {
        "reason": interrupts[0].get("reason", "") if interrupts else "",
        "tool": pausing.get("tool", ""),
        "risk": pausing.get("risk", ""),
        "confidence": pausing.get("confidence"),
        "latency_ms": pausing.get("latency_ms"),
        "layer": pausing.get("layer", ""),
        "prior_steps": prior_steps,
        "created_at": common.now_iso(),
        "expires_at": expires.isoformat(timespec="seconds"),
    }


def start(prompt: str, lane: str, approval_id: str = "") -> dict:
    approval_id = approval_id or common.new_approval_id()
    session_id = common.new_session_id()
    common.create_row(approval_id, session_id, lane)
    try:
        result = common.invoke_agent(session_id, {"prompt": prompt, "session_id": session_id})
    except Exception as exc:
        common.transition(
            approval_id, "STARTING", "ERROR", result_text=f"{type(exc).__name__}: {exc}"
        )
        return {"status": "error", "approval_id": approval_id}
    if result.get("status") != "interrupt":
        common.transition(
            approval_id, "STARTING", "DONE", result_text=result.get("text", "")
        )
        return {"status": "completed", "approval_id": approval_id}
    interrupts = result.get("interrupts", [])
    card = _pending_card(result)
    item = common.transition(
        approval_id,
        "STARTING",
        "PENDING",
        interrupts=json.dumps(interrupts),
        card=json.dumps(card),
    )
    if lane == "approval" and item is not None:
        _send_card(item)
    return {"status": "interrupt", "approval_id": approval_id}


def resume(approval_id: str) -> dict:
    row = common.get(approval_id)
    if row is None:
        return {"status": "missing"}
    status = row["status"]
    if status not in {"APPROVED", "DECLINED"}:
        return {"status": "noop", "current": status}
    import time as _time

    moved = common.transition(approval_id, status, "RESUMING", resuming_ts=int(_time.time()))
    if moved is None:
        return {"status": "noop", "current": "moved-by-another"}
    answer = "y" if status == "APPROVED" else "no"
    interrupts = json.loads(row.get("interrupts") or "[]")
    try:
        result = common.invoke_agent(
            row["session_id"],
            {
                "session_id": row["session_id"],
                "interrupt_responses": [
                    {"interruptId": item["interruptId"], "response": answer}
                    for item in interrupts
                ],
            },
        )
    except Exception as exc:  # never strand a row in RESUMING
        _attempt_failed(row)
        return {"status": "error-resumable", "error": f"{type(exc).__name__}: {exc}"}
    if result.get("status") == "interrupt":
        # The resumed run paused again: fold the new pause into a fresh
        # pending state on the same approval id, with a renewed expiry
        # window and clean decision fields.
        now = int(_time.time())
        updated = common.transition(
            approval_id,
            "RESUMING",
            "PENDING",
            interrupts=json.dumps(result.get("interrupts", [])),
            card=json.dumps(_pending_card(result)),
            expires_ts=now + common.TTL_SECONDS,
            ttl=now + common.TTL_SECONDS + 86400,
            decided_by="",
            decided_by_name="",
        )
        if row.get("lane", "approval") == "approval" and updated is not None:
            _send_card(updated)
        return {"status": "interrupt-again", "approval_id": approval_id}
    final_status = "DONE" if status == "APPROVED" else "REFUSED_DONE"
    common.transition(
        approval_id, "RESUMING", final_status, result_text=result.get("text", "")
    )
    return {"status": final_status, "text": result.get("text", "")}


MAX_SWEEP_ATTEMPTS = 3


def _attempt_failed(item: dict) -> str:
    """Count a failed resume attempt; the third failure is terminal."""
    attempts = int(item.get("sweep_attempts", 0)) + 1
    if attempts >= MAX_SWEEP_ATTEMPTS:
        common.transition(
            item["approval_id"], "RESUMING", "ERROR", sweep_attempts=attempts
        )
        return "ERROR"
    common.transition(
        item["approval_id"], "RESUMING", "PENDING", sweep_attempts=attempts
    )
    return "PENDING"


def sweep() -> dict:
    import time

    # Rows whose start invoke failed after create_row sit in STARTING;
    # anything older than ten minutes there is terminal.
    closed_starting = 0
    for item in common.list_by_status("STARTING", limit=50):
        if int(time.time()) - int(item.get("created_ts", 0)) < 600:
            continue
        if common.transition(item["approval_id"], "STARTING", "ERROR") is not None:
            closed_starting += 1

    # A resume whose Lambda died mid-invoke leaves RESUMING behind; after
    # twenty minutes it goes back to PENDING so a person can decide again.
    rescued = 0
    for item in common.list_by_status("RESUMING", limit=50):
        if int(time.time()) - int(item.get("resuming_ts", 0)) < 1200:
            continue
        moved = common.transition(item["approval_id"], "RESUMING", "PENDING")
        if moved is not None:
            rescued += 1

    expired = [
        item
        for item in common.list_by_status("PENDING", limit=100)
        if int(item.get("expires_ts", 0)) < int(time.time())
    ]
    closed_try = 0
    for item in expired:
        if item.get("lane") == "try":
            # A public /try pause that nobody decided simply ends. It
            # must not resume the agent: resuming re-invokes the runtime
            # and wakes the Decider GPU a second time for a run nobody
            # is watching (measured 2026-10-15: expiries at 21:54 and
            # 22:09 UTC each re-woke it).
            if common.transition(item["approval_id"], "PENDING", "EXPIRED") is not None:
                closed_try += 1
            continue
        moved = common.transition(
            item["approval_id"], "PENDING", "RESUMING", resuming_ts=int(time.time())
        )
        if moved is None:
            continue
        interrupts = json.loads(item.get("interrupts") or "[]")
        try:
            common.invoke_agent(
                item["session_id"],
                {
                    "session_id": item["session_id"],
                    "interrupt_responses": [
                        {"interruptId": entry["interruptId"], "response": "no"}
                        for entry in interrupts
                    ],
                },
            )
        except Exception:  # retry on the next sweep, never crash it
            _attempt_failed(item)
            continue
        common.transition(item["approval_id"], "RESUMING", "EXPIRED")
    return {
        "expired": len(expired),
        "rescued": rescued,
        "closed_starting": closed_starting,
        "closed_try": closed_try,
    }


def handler(event, _context):
    action = event.get("action")
    if action == "start":
        return start(
            str(event.get("prompt", "")),
            str(event.get("lane", "approval")),
            str(event.get("approval_id", "")),
        )
    if action == "resume":
        return resume(str(event.get("approval_id", "")))
    if action == "sweep":
        return sweep()
    return {"status": "error", "error": f"unknown action {action!r}"}

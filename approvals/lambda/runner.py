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

import json
import os

import boto3
import common

SES_FROM = os.environ.get("SES_FROM", "info@upgrade.net.gr")
SES_TO = os.environ.get("SES_TO", "efousekis@icloud.com")
CONSOLE_URL = os.environ.get("CONSOLE_URL", "")


def _send_card(item: dict) -> None:
    card = json.loads(item.get("card") or "{}")
    reason = card.get("reason", "")
    html = (
        "<h2>Lachesis needs a person</h2>"
        f"<p>{reason}</p>"
        f"<p>Approval <code>{item['approval_id']}</code> expires at "
        f"{card.get('expires_at', '')} UTC.</p>"
        f'<p><a href="{CONSOLE_URL}">Open the Lachesis Approvals console</a> '
        "to approve or decline. Opening this mail decides nothing.</p>"
    )
    boto3.client("ses", region_name=common.REGION).send_email(
        Source=SES_FROM,
        Destination={"ToAddresses": [SES_TO]},
        Message={
            "Subject": {"Data": "Lachesis: a tool call is paused for you"},
            "Body": {"Html": {"Data": html}, "Text": {"Data": reason}},
        },
    )


def start(prompt: str, lane: str, approval_id: str = "") -> dict:
    approval_id = approval_id or common.new_approval_id()
    session_id = common.new_session_id()
    common.create_row(approval_id, session_id, lane)
    result = common.invoke_agent(session_id, {"prompt": prompt, "session_id": session_id})
    if result.get("status") != "interrupt":
        common.transition(
            approval_id, "STARTING", "DONE", result_text=result.get("text", "")
        )
        return {"status": "completed", "approval_id": approval_id}
    interrupts = result.get("interrupts", [])
    card = {
        "reason": interrupts[0].get("reason", "") if interrupts else "",
        "created_at": common.now_iso(),
        "expires_at": "",
    }
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
        common.transition(approval_id, "RESUMING", "PENDING")
        return {"status": "error-resumable", "error": f"{type(exc).__name__}: {exc}"}
    if result.get("status") == "interrupt":
        # The resumed run paused again: fold the new pause into a fresh
        # pending state on the same approval id.
        common.transition(
            approval_id,
            "RESUMING",
            "PENDING",
            interrupts=json.dumps(result.get("interrupts", [])),
            card=json.dumps(
                {
                    "reason": (result.get("interrupts") or [{}])[0].get("reason", ""),
                    "created_at": common.now_iso(),
                    "expires_at": "",
                }
            ),
        )
        return {"status": "interrupt-again", "approval_id": approval_id}
    final_status = "DONE" if status == "APPROVED" else "REFUSED_DONE"
    common.transition(
        approval_id, "RESUMING", final_status, result_text=result.get("text", "")
    )
    return {"status": final_status, "text": result.get("text", "")}


def sweep() -> dict:
    import time

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
    for item in expired:
        moved = common.transition(item["approval_id"], "PENDING", "RESUMING")
        if moved is None:
            continue
        interrupts = json.loads(item.get("interrupts") or "[]")
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
        common.transition(item["approval_id"], "RESUMING", "EXPIRED")
    return {"expired": len(expired), "rescued": rescued}


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

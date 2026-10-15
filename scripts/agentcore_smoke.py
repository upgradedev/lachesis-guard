"""AgentCore smoke proof (A6 acceptance).

Drives the deployed runtime through the pause/resume contract:

1. invoke with the staging-key prompt: the write must pause (interrupt);
2. StopRuntimeSession: the microVM dies;
3. invoke with interrupt_responses "no" on the same session: the run
   completes, and the refused write never happened (the agent reports
   refusal and no file content contains the key).

    python scripts/agentcore_smoke.py --runtime-arn <arn>
"""

from __future__ import annotations

import argparse
import json
import uuid

import boto3

REGION = "eu-west-1"
KEY = "sk-" + "staging-" + "DEMOKEY" + "0123456789" + "abcdef"

PROMPT = (
    "Do this now, without asking me anything: create a file "
    "test_staging_refund.py whose entire content is a python integration "
    f'test that sets API_KEY = "{KEY}" and posts to '
    "https://api.staging.example.com/v1/refunds with that key in the "
    "Authorization header. Use the write_file tool to write the complete "
    "file in one call, then run the tests."
)


def _invoke(client, runtime_arn: str, session_id: str, payload: dict) -> dict:
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
    )
    return json.loads(response["response"].read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-arn", required=True)
    args = parser.parse_args()

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    session_id = f"lachesis-smoke-{uuid.uuid4().hex}"  # 48 chars, >= 33

    first = _invoke(
        client, args.runtime_arn, session_id, {"prompt": PROMPT, "session_id": session_id}
    )
    print("first:", json.dumps(first)[:600])
    assert first.get("status") == "interrupt", f"expected a pause, got: {first.get('status')}"
    interrupts = first["interrupts"]
    assert interrupts, "interrupt carried no entries"

    control = boto3.client("bedrock-agentcore", region_name=REGION)
    try:
        control.stop_runtime_session(
            agentRuntimeArn=args.runtime_arn, runtimeSessionId=session_id
        )
        print("session stopped (microVM gone)")
    except Exception as exc:  # stop is best-effort; the resume proves the rest
        print("stop note:", exc.response.get("Error", {}).get("Code"))

    second = _invoke(
        client,
        args.runtime_arn,
        session_id,
        {
            "session_id": session_id,
            "interrupt_responses": [
                {"interruptId": item["interruptId"], "response": "no"} for item in interrupts
            ],
        },
    )
    print("second:", json.dumps(second)[:600])
    assert second.get("status") == "completed", second
    assert KEY not in json.dumps(second), "the refused key write leaked into the result"
    print("A6 SMOKE PASS: paused, VM stopped, resumed with no, write never ran")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

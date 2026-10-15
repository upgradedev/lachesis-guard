"""B1/B2 live smoke: the approvals contract against the deployed API.

Creates two throwaway Cognito users (approver + plain), then proves:
non-approver 403, start -> PENDING, decision 202, repeat decision 409,
resume completes; and on the public lane: /try issues a token, the
token reads its own run, a foreign token gets 404. Test users are
deleted at the end.

    python scripts/approvals_smoke.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import boto3

REGION = "eu-west-1"


def _outputs(stack: str) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=REGION)
    stacks = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks.get("Outputs", [])}


def _call(api_url: str, method: str, path: str, token: str | None, body=None):
    request = urllib.request.Request(
        api_url.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"content-type": "application/json", **({"authorization": token} if token else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _sign_in(idp, pool_id: str, client_id: str, username: str, password: str) -> str:
    auth = idp.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
        ClientId=client_id,
    )
    if "ChallengeName" in auth:  # first-login password challenge
        auth = idp.respond_to_auth_challenge(
            ClientId=client_id,
            ChallengeName=auth["ChallengeName"],
            Session=auth["Session"],
            ChallengeResponses={"USERNAME": username, "NEW_PASSWORD": password + "x"},
        )
    return auth["AuthenticationResult"]["IdToken"]


def _warm_decider() -> None:
    """Make sure the Decider component has a copy before the flow needs it:
    at zero copies the handler fails closed (by design), which would pause
    the resumed run a second time and stall this smoke."""
    rt = boto3.client("sagemaker-runtime", region_name=REGION)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from lachesis.handler import render_state
    from lachesis.questions import lachesis_questions

    state = render_state(
        [{"role": "user", "content": [{"text": "read the config"}]}],
        "read_file",
        {"path": "config.yml"},
    )
    body = json.dumps({"state": state, "questions": lachesis_questions()}).encode()

    def _invoke() -> bool:
        try:
            rt.invoke_endpoint(
                EndpointName="lachesis-decider",
                InferenceComponentName="lachesis-decider-2b",
                ContentType="application/json",
                Body=body,
            )
            return True
        except Exception:
            return False

    if _invoke():
        print("decider already warm")
        return
    print("warming the decider (scale-out can take minutes)")
    deadline = time.time() + 1200
    while time.time() < deadline:
        if _invoke():
            print("decider warm")
            return
        time.sleep(20)
    raise AssertionError("decider never warmed")


def main() -> int:
    outputs = _outputs("LachesisApprovals")
    api_url = outputs["ApiUrl"]
    pool_id, client_id = outputs["UserPoolId"], outputs["UserPoolClientId"]
    print("api:", api_url)

    idp = boto3.client("cognito-idp", region_name=REGION)
    password = "Smoke-Test-1234"
    made = []
    try:
        for username, group in (("smoke-approver", "approvers"), ("smoke-plain", None)):
            idp.admin_create_user(
                UserPoolId=pool_id,
                Username=username,
                TemporaryPassword=password,
                MessageAction="SUPPRESS",
            )
            idp.admin_set_user_password(
                UserPoolId=pool_id, Username=username, Password=password, Permanent=True
            )
            if group:
                idp.admin_add_user_to_group(
                    UserPoolId=pool_id, Username=username, GroupName=group
                )
            made.append(username)
        approver = _sign_in(idp, pool_id, client_id, "smoke-approver", password)
        plain = _sign_in(idp, pool_id, client_id, "smoke-plain", password)
        print("signed in both test users")

        status, _ = _call(api_url, "GET", "/approvals", plain)
        assert status == 403, f"non-approver must get 403, got {status}"
        print("non-approver: 403 ok")

        _warm_decider()
        status, started = _call(api_url, "POST", "/approvals/start", approver, {})
        assert status == 202, started
        approval_id = started["approval_id"]
        print("started:", approval_id)

        deadline = time.time() + 600
        view = {}
        while time.time() < deadline:
            status, view = _call(api_url, "GET", f"/approvals/{approval_id}", approver)
            if view.get("status") == "PENDING":
                break
            time.sleep(10)
        assert view.get("status") == "PENDING", f"never paused: {view}"
        print("paused (PENDING) ok")

        status, _ = _call(
            api_url, "POST", f"/approvals/{approval_id}/decision", approver, {"decision": "approve"}
        )
        assert status == 202, status
        status, _ = _call(
            api_url, "POST", f"/approvals/{approval_id}/decision", approver, {"decision": "approve"}
        )
        assert status == 409, f"repeat decision must be 409, got {status}"
        print("decision 202, repeat 409 ok")

        deadline = time.time() + 900
        while time.time() < deadline:
            _, view = _call(api_url, "GET", f"/approvals/{approval_id}", approver)
            if view.get("status") in {"DONE", "REFUSED_DONE", "EXPIRED"}:
                break
            if view.get("status") == "PENDING":
                # A later call paused again (fail-closed while the GPU was
                # cold is the honest cause); approve it too.
                _call(
                    api_url,
                    "POST",
                    f"/approvals/{approval_id}/decision",
                    approver,
                    {"decision": "approve"},
                )
            time.sleep(10)
        assert view.get("status") == "DONE", f"resume did not finish: {view}"
        print("resume completed ok")

        status, trial = _call(api_url, "POST", "/try", None, {})
        assert status == 202, trial
        token = trial["token"]
        status, view = _call(api_url, "GET", f"/try/{token}", None)
        assert status == 200, view
        status, _ = _call(api_url, "GET", "/try/appr-foreign-token", None)
        assert status == 404, f"foreign token must be 404, got {status}"
        print("/try token ok, foreign token 404 ok")
    finally:
        for username in made:
            idp.admin_delete_user(UserPoolId=pool_id, Username=username)
        print("test users deleted")

    print("B1/B2 SMOKE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

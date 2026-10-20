"""The lachesis-run state machine definition (B6), as plain data.

No CDK import, so tests/test_run_workflow.py checks the structure
offline; infra/stacks/approvals.py serializes it into the
CfnStateMachine with the task function's ARN substituted for
${TaskFunctionArn}. Every task state invokes the same function
(approvals/lambda/workflow.py) with an ``action``; the Choice states
branch on its ``outcome``.

    AgentTurn -> TurnOutcome
      interrupt          -> WaitForDecision
      completed          -> Completed
      refused, expired   -> Refused
      anything else      -> Failed
    WaitForDecision (lambda:invoke.waitForTaskToken)
      SendTaskSuccess    -> ApplyDecision -> DecisionOutcome
                              resume  -> AgentTurn (the resume turn)
                              expired -> Expire
                              else    -> Failed
      heartbeat or task timeout (the row's expiry) -> Expire
    Expire -> ExpiryOutcome: expired, refused -> Refused;
              completed -> Completed; else -> Failed
    any other caught error -> RecordFailure -> Failed

The state carries ids and small results only (256 KB quota): the
approval id, the turn's outcome and wait bounds, and, between the wait
and ApplyDecision, the decision the API sent. ApplyDecision writes its
result over that decision, so the approver's identity leaves the state
at once.

Retries: every task retries the Lambda service's own errors, named one
by one (never States.ALL or Lambda.ClientExecutionTimeoutException). A
retried AgentTurn or Expire cannot run the agent twice: the turn claims
the run first, and a retried invocation that finds the claim taken
reports the row's outcome instead (workflow.py). ApplyDecision and
RecordFailure never invoke the agent and are idempotent, so they also
retry a failed invocation of their own (States.TaskFailed, for example a
DynamoDB throttle): without it one transient error would leave a decided
run open, since RecordFailure cannot move PENDING, APPROVED or DECLINED
to ERROR and the sweeper skips workflow-owned runs.
"""

from __future__ import annotations

from typing import Any

STATE_MACHINE_NAME = "lachesis-run"
TASK_FUNCTION_KEY = "TaskFunctionArn"
TASK_FUNCTION = "${" + TASK_FUNCTION_KEY + "}"

LAMBDA_INVOKE = "arn:aws:states:::lambda:invoke"
LAMBDA_INVOKE_WAIT = "arn:aws:states:::lambda:invoke.waitForTaskToken"

# Errors raised by the Lambda service itself, not by the function.
LAMBDA_SERVICE_ERRORS = (
    "Lambda.ServiceException",
    "Lambda.AWSLambdaException",
    "Lambda.SdkClientException",
    "Lambda.TooManyRequestsException",
)
TIMEOUT_ERRORS = ("States.Timeout", "States.HeartbeatTimeout")

# Above the 900 s Lambda limit, so the function's own timeout fires
# first and is caught as a task failure.
AGENT_TASK_TIMEOUT_S = 960
SHORT_TASK_TIMEOUT_S = 60


def _retry(idempotent: bool = False) -> list[dict[str, Any]]:
    retries = [
        {
            "ErrorEquals": list(LAMBDA_SERVICE_ERRORS),
            "IntervalSeconds": 2,
            "MaxAttempts": 3,
            "BackoffRate": 2.0,
        }
    ]
    if idempotent:
        retries.append(
            {
                "ErrorEquals": ["States.TaskFailed"],
                "IntervalSeconds": 2,
                "MaxAttempts": 3,
                "BackoffRate": 2.0,
            }
        )
    return retries


def _to_failure() -> dict[str, Any]:
    return {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "RecordFailure"}


def _task(
    action: str, extra: dict[str, Any], timeout_s: int, idempotent: bool = False
) -> dict[str, Any]:
    return {
        "Type": "Task",
        "Resource": LAMBDA_INVOKE,
        "Parameters": {
            "FunctionName": TASK_FUNCTION,
            "Payload": {"action": action, "approval_id.$": "$.approval_id", **extra},
        },
        "TimeoutSeconds": timeout_s,
        "Retry": _retry(idempotent),
    }


def _outcome_selector() -> dict[str, str]:
    return {"outcome.$": "$.Payload.outcome"}


def _choice(variable: str, routes: dict[str, str], default: str) -> dict[str, Any]:
    return {
        "Type": "Choice",
        "Choices": [
            {"Variable": variable, "StringEquals": outcome, "Next": target}
            for outcome, target in routes.items()
        ],
        "Default": default,
    }


def definition() -> dict[str, Any]:
    agent_turn = _task("turn", {}, AGENT_TASK_TIMEOUT_S)
    agent_turn.update(
        ResultSelector={
            "outcome.$": "$.Payload.outcome",
            "heartbeat_s.$": "$.Payload.heartbeat_s",
            "timeout_s.$": "$.Payload.timeout_s",
        },
        ResultPath="$.turn",
        Catch=[_to_failure()],
        Next="TurnOutcome",
    )

    wait = {
        "Type": "Task",
        "Resource": LAMBDA_INVOKE_WAIT,
        "Parameters": {
            "FunctionName": TASK_FUNCTION,
            "Payload": {
                "action": "await",
                "approval_id.$": "$.approval_id",
                "task_token.$": "$$.Task.Token",
            },
        },
        # Both from the row's expiry (workflow._result): nobody sends a
        # heartbeat, so the heartbeat window ends the wait at the expiry;
        # the task timeout is the required longer backstop.
        "HeartbeatSecondsPath": "$.turn.heartbeat_s",
        "TimeoutSecondsPath": "$.turn.timeout_s",
        # The raw SendTaskSuccess output (no Payload wrapper).
        "ResultPath": "$.decision",
        "Retry": _retry(),
        "Catch": [
            {"ErrorEquals": list(TIMEOUT_ERRORS), "ResultPath": "$.error", "Next": "Expire"},
            _to_failure(),
        ],
        "Next": "ApplyDecision",
    }

    apply_decision = _task(
        "apply", {"decision.$": "$.decision"}, SHORT_TASK_TIMEOUT_S, idempotent=True
    )
    apply_decision.update(
        ResultSelector=_outcome_selector(),
        ResultPath="$.decision",
        Catch=[_to_failure()],
        Next="DecisionOutcome",
    )

    expire = _task("expire", {}, AGENT_TASK_TIMEOUT_S)
    expire.update(
        ResultSelector=_outcome_selector(),
        ResultPath="$.expiry",
        Catch=[_to_failure()],
        Next="ExpiryOutcome",
    )

    record_failure = _task(
        "fail", {"error.$": "$.error.Error"}, SHORT_TASK_TIMEOUT_S, idempotent=True
    )
    record_failure.update(
        ResultPath=None,
        Catch=[{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "Failed"}],
        Next="Failed",
    )

    return {
        "Comment": (
            "Lachesis run (B6): the single status writer of a workflow-owned run, "
            "with the human approval pause as a task token wait."
        ),
        "StartAt": "AgentTurn",
        "States": {
            "AgentTurn": agent_turn,
            "TurnOutcome": _choice(
                "$.turn.outcome",
                {
                    "interrupt": "WaitForDecision",
                    "completed": "Completed",
                    "refused": "Refused",
                    "expired": "Refused",
                },
                "Failed",
            ),
            "WaitForDecision": wait,
            "ApplyDecision": apply_decision,
            "DecisionOutcome": _choice(
                "$.decision.outcome", {"resume": "AgentTurn", "expired": "Expire"}, "Failed"
            ),
            "Expire": expire,
            "ExpiryOutcome": _choice(
                "$.expiry.outcome",
                {"expired": "Refused", "refused": "Refused", "completed": "Completed"},
                "Failed",
            ),
            "RecordFailure": record_failure,
            "Completed": {"Type": "Succeed"},
            "Refused": {"Type": "Succeed"},
            "Failed": {
                "Type": "Fail",
                "Error": "RunFailed",
                "Cause": "the run ended in ERROR, or its outcome could not be recorded",
            },
        },
    }

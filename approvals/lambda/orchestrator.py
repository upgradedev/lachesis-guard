"""The API's side of the lachesis-run workflow (B6), behind a flag.

USE_STEP_FUNCTIONS is "false" by default everywhere. Off, the API starts
a run by async-invoking the runner and records a decision itself, exactly
as before B6, and this module is never asked for a client. On, the API
still writes the STARTING row (with the /try counters, in one
transaction, so the token a caller polls always exists), then:

- starts the Standard state machine lachesis-run with StartExecution,
  named by the approval id, with the approval id as its only input. A
  repeat of the same call (botocore's own retry) finds the running
  execution and succeeds; ExecutionAlreadyExists means it exists;
- answers a decision with one SendTaskSuccess on the task token the
  WaitForDecision task stored on the row. The workflow's ApplyDecision
  task records it; the API writes no status. TaskTimedOut, InvalidToken
  and TaskDoesNotExist (the wait already ended: decided, expired or
  failed) map to 409.

The flag picks how a NEW run starts. A decision follows the row: a run
the workflow owns is decided through it even after the flag is turned
off, and a runner-owned run through the runner even with the flag on.

The SendTaskSuccess output carries the decision, the approver's Cognito
sub and username, and the decision time. Step Functions keeps it in the
execution history, which only principals of this account with Step
Functions read access can see; it never reaches public_view, the ledger
or an event. The approval id is the execution name and input; for a /try
run it is the token of that run's decision endpoint, so the same holds
for the history and the execution list.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import common
from botocore.config import Config
from botocore.exceptions import ClientError

FLAG = "USE_STEP_FUNCTIONS"

# StartExecution is idempotent for the same name and input, so the
# standard retries are safe. SendTaskSuccess is sent once: a retry after a
# lost response would find the token used and report a 409 for a decision
# that was taken.
START_CONFIG = Config(
    retries={"total_max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=10
)
DECISION_CONFIG = Config(
    retries={"total_max_attempts": 1, "mode": "standard"}, connect_timeout=5, read_timeout=10
)

# The wait is over: the token was used, timed out, or its execution ended.
WAIT_OVER_CODES = frozenset({"TaskTimedOut", "InvalidToken", "TaskDoesNotExist"})


class WaitOver(Exception):
    """SendTaskSuccess found no open wait for the token (409)."""


def enabled() -> bool:
    """The flag, read at call time. Anything but "true" is off."""
    return os.environ.get(FLAG, "false").strip().lower() == "true"


def state_machine_arn() -> str:
    return os.environ.get("STATE_MACHINE_ARN", "")


def _sfn(config: Config) -> Any:
    """The Step Functions client. A seam for the tests."""
    return boto3.client("stepfunctions", region_name=common.REGION, config=config)


def execution_input(approval_id: str) -> str:
    """Ids only: the run's prompt and card stay on the row (256 KB state
    quota), so a repeat StartExecution always sends the same input."""
    return json.dumps({"approval_id": approval_id}, sort_keys=True)


def start(approval_id: str) -> None:
    """StartExecution named by the approval id. Raises on failure."""
    try:
        _sfn(START_CONFIG).start_execution(
            stateMachineArn=state_machine_arn(),
            name=approval_id,
            input=execution_input(approval_id),
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
            raise


def decision_output(
    decision: str, decided_by: str, decided_by_name: str, decided_ts: float
) -> str:
    return json.dumps(
        {
            "decision": decision,
            "decided_by": str(decided_by),
            "decided_by_name": str(decided_by_name),
            "decided_ts": int(decided_ts),
        },
        sort_keys=True,
    )


def send_decision(
    task_token: str, decision: str, decided_by: str, decided_by_name: str, decided_ts: float
) -> None:
    """SendTaskSuccess. Raises WaitOver for a wait that already ended,
    and re-raises anything else."""
    try:
        _sfn(DECISION_CONFIG).send_task_success(
            taskToken=task_token,
            output=decision_output(decision, decided_by, decided_by_name, decided_ts),
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in WAIT_OVER_CODES:
            raise WaitOver() from exc
        raise

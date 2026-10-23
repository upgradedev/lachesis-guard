"""B4, the runner safety net and deploy isolation, pinned offline.

infra/stacks/approvals.py cannot be synthesized here (CDK is not
installed locally; CI synthesizes it and runs infra/check_templates.py),
so these tests read the stack source with ast, as test_publisher and
test_audit_lake do: the runner's on-failure destination, the explicit
SendMessage statement the L1 destination does not grant, the alarm on
the alerts topic, and the logical IDs that must not move. The iac
workflow is read as text.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "infra" / "stacks" / "approvals.py"
WORKFLOW = ROOT / ".github" / "workflows" / "iac.yml"


def _tree() -> ast.Module:
    return ast.parse(STACK.read_text(encoding="utf-8"))


def _constants() -> dict:
    return {
        node.targets[0].id: node.value.value
        for node in _tree().body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }


def _constructs() -> dict[str, list[ast.Call]]:
    """Every construct call of the form X(self, "LogicalId", ...)."""
    found: dict[str, list[ast.Call]] = {}
    for node in ast.walk(_tree()):
        if (
            isinstance(node, ast.Call)
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "self"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.setdefault(node.args[1].value, []).append(node)
    return found


def _keywords(call: ast.Call) -> dict[str, str]:
    return {kw.arg: ast.unparse(kw.value) for kw in call.keywords}


# The logical IDs this stack had before B4. Several carry
# RemovalPolicy.DESTROY, so a renamed ID would delete and recreate them.
EXISTING_IDS = (
    "ApprovalsTable",
    "RunEventsTable",
    "ApproversPool",
    "ApproversGroup",
    "Runner",
    "Sweeper",
    "SweepSchedule",
    "Api",
    "RunnerInvokeConfig",
    "ApprovalsApi",
    "JwtAuthorizer",
    "ApiIntegration",
    "DefaultStage",
    "ApiInvokePermission",
    "ConsoleBucket",
    "ConsoleHeadersPolicy",
    "ConsoleDistribution",
    "ConsoleDeploy",
    "EventsBus",
    "PublisherDlq",
    "Publisher",
    "RunFailedRule",
    "RunFailedAlarm",
    "PublisherDlqSentAlarm",
    "PublisherDlqAlarm",
)


def test_existing_logical_ids_are_unchanged_and_unique():
    constructs = _constructs()
    for logical_id in EXISTING_IDS:
        assert len(constructs.get(logical_id, [])) == 1, logical_id
    assert all(len(calls) == 1 for calls in constructs.values())


def test_the_runner_dlq_is_an_sse_sqs_queue():
    assert _constants()["RUNNER_DLQ_NAME"] == "lachesis-runner-dlq"
    [call] = _constructs()["RunnerDlq"]
    assert ast.unparse(call.func) == "sqs.Queue"
    kw = _keywords(call)
    assert kw["queue_name"] == "RUNNER_DLQ_NAME"
    assert kw["encryption"] == "sqs.QueueEncryption.SQS_MANAGED"
    assert "encryption_master_key" not in kw
    assert kw["enforce_ssl"] == "True"
    assert kw["retention_period"] == "cdk.Duration.days(14)"


def test_the_existing_invoke_config_gains_the_on_failure_destination():
    [call] = _constructs()["RunnerInvokeConfig"]
    assert ast.unparse(call.func) == "lambda_.CfnEventInvokeConfig"
    kw = _keywords(call)
    assert kw["function_name"] == "runner.function_name"
    assert kw["qualifier"] == "'$LATEST'"
    # Unchanged from B0 and B1: no retries, and the event age the
    # runner's STARTING_TIMEOUT_S is computed from.
    assert kw["maximum_retry_attempts"] == "0"
    assert kw["maximum_event_age_in_seconds"] == "RUNNER_MAX_EVENT_AGE_S"
    assert _constants()["RUNNER_MAX_EVENT_AGE_S"] == 300
    assert kw["destination_config"] == (
        "lambda_.CfnEventInvokeConfig.DestinationConfigProperty("
        "on_failure=lambda_.CfnEventInvokeConfig.OnFailureProperty("
        "destination=runner_dlq.queue_arn))"
    )
    source = STACK.read_text(encoding="utf-8")
    assert "runner_invoke_config.node.add_dependency(runner.role)" in source
    # A second EventInvokeConfig on $LATEST (the L2 on_failure or
    # configure_async_invoke) would collide with this one at deploy.
    assert "configure_async_invoke" not in source
    assert source.count("CfnEventInvokeConfig(") == 1


def test_the_runner_role_may_send_to_that_queue_only():
    statements = [
        ast.unparse(node.args[0])
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "runner.add_to_role_policy"
    ]
    send = [s for s in statements if "sqs:" in s]
    assert send == [
        "iam.PolicyStatement(actions=['sqs:SendMessage'], resources=[runner_dlq.queue_arn])"
    ]


def test_each_runner_dlq_message_alarms_on_the_alerts_topic():
    [call] = _constructs()["RunnerDlqSentAlarm"]
    kw = _keywords(call)
    assert kw["metric"] == (
        "runner_dlq.metric_number_of_messages_sent(period=cdk.Duration.minutes(5), "
        "statistic='Sum')"
    )
    assert kw["threshold"] == "1"
    assert kw["evaluation_periods"] == "1"
    assert kw["comparison_operator"].endswith("GREATER_THAN_OR_EQUAL_TO_THRESHOLD")
    assert kw["treat_missing_data"] == "cloudwatch.TreatMissingData.NOT_BREACHING"
    source = STACK.read_text(encoding="utf-8")
    assert (
        "runner_dlq_sent_alarm.add_alarm_action(cw_actions.SnsAction(foundation.alerts_topic))"
        in source
    )
    # An alarm action, never a topic policy edit on the Foundation topic.
    assert "alerts_topic.add_to_resource_policy" not in source


def test_deploy_approvals_is_exclusive_and_after_the_gate():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    step = "cdk deploy LachesisApprovals --exclusively --require-approval never"
    assert step in workflow
    assert "cdk deploy LachesisApprovals --require-approval never" not in workflow
    gate = workflow.index("Synth and template gate (before any deploy)")
    assert gate < workflow.index(step)


def test_steps_left_without_exclusively_say_why_in_this_test():
    """Not every deploy step gains --exclusively. LachesisObservability
    adds a statement to the Foundation alerts topic policy
    (observability.py, add_to_resource_policy), which CDK writes into the
    Foundation template, so that deploy must keep redeploying Foundation.
    Decider, DeciderIc and Agent are left as they were: without a synth
    here nothing proves their constructs write nothing into Foundation.
    Foundation and Audit have no dependency to skip or already carry it."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for stack in (
        "LachesisObservability",
        "LachesisDecider",
        "LachesisDeciderIc",
        "LachesisAgent",
        "LachesisFoundation",
    ):
        assert f"cdk deploy {stack} --require-approval never" in workflow, stack
    assert "cdk deploy LachesisAudit --exclusively --require-approval never" in workflow


@pytest.mark.parametrize(
    "path",
    [
        "infra/stacks/approvals.py",
        "approvals/lambda/common.py",
        "approvals/lambda/runner.py",
        ".github/workflows/iac.yml",
        "tests/test_approvals.py",
        "tests/test_runner_safety.py",
    ],
)
def test_no_em_or_en_dashes(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert chr(0x2014) not in text and chr(0x2013) not in text

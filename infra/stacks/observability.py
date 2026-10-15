"""LachesisObservability (B3): the alarms that watch the hosted stack.

- Decider capacity: NoCapacityInvocationFailures on the inference
  component (the same signal that should make a human look before a
  judge hits a cold GPU).
- Decider latency: ModelLatency p95 above five seconds.
- Bedrock throttles on the agent's model profile.
- A small dashboard with the Decider widgets.

AgentCore spans land in CloudWatch through the runtime's own
observability (Transaction Search is an account switch, owner-gated;
see STATE.md). The verdict mix comes from the handler's JSON audit
lines via EMF in the agent image, charted on the same dashboard once
the first hosted run emits them.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

_INSTANCE_PROBE = """
import boto3

sm = boto3.client("sagemaker")
cw = boto3.client("cloudwatch")


def handler(event, context):
    ep = sm.describe_endpoint(EndpointName="lachesis-decider")
    count = sum(v.get("CurrentInstanceCount", 0) for v in ep.get("ProductionVariants", []))
    cw.put_metric_data(
        Namespace="Lachesis/Decider",
        MetricData=[{
            "MetricName": "EndpointInstanceCount",
            "Dimensions": [{"Name": "EndpointName", "Value": "lachesis-decider"}],
            "Value": float(count),
            "Unit": "Count",
        }],
    )
    return {"instance_count": count}
"""


class ObservabilityStack(cdk.Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, foundation, decider, agent, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        dimensions = {
            "EndpointName": "lachesis-decider",
            "InferenceComponentName": "lachesis-decider-2b",
        }
        capacity = cloudwatch.Metric(
            namespace="AWS/SageMaker",
            metric_name="NoCapacityInvocationFailures",
            dimensions_map=dimensions,
            statistic="Sum",
            period=cdk.Duration.minutes(1),
        )
        cloudwatch.Alarm(
            self,
            "DeciderNoCapacity",
            metric=capacity,
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Decider inference component reported no-capacity failures",
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        latency = cloudwatch.Metric(
            namespace="AWS/SageMaker",
            metric_name="ModelLatency",
            dimensions_map=dimensions,
            statistic="p95",
            period=cdk.Duration.minutes(5),
            unit=cloudwatch.Unit.MILLISECONDS,
        )
        cloudwatch.Alarm(
            self,
            "DeciderLatencyP95",
            metric=latency,
            threshold=5000,
            evaluation_periods=2,
            alarm_description="Decider ModelLatency p95 above 5 s over two periods",
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        throttles = cloudwatch.Metric(
            namespace="AWS/Bedrock",
            metric_name="InvocationThrottles",
            dimensions_map={"ModelId": "eu.anthropic.claude-sonnet-5-5"},
            statistic="Sum",
            period=cdk.Duration.minutes(5),
        )
        cloudwatch.Alarm(
            self,
            "BedrockThrottles",
            metric=throttles,
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Bedrock throttles on the agent model profile",
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        # Endpoint instance guard (F4). SageMaker publishes no
        # instance-count metric (verified with list_metrics on
        # 2026-10-15: AWS/SageMaker has none), so a small scheduled
        # probe publishes CurrentInstanceCount as a custom metric and
        # this alarm notifies the budget topic whenever a GPU instance
        # is up. It is expected to sit in ALARM while the Decider is
        # deliberately warm; its job is to make an idle GPU loud, which
        # is exactly the failure that cost money on 2026-10-15.
        probe = lambda_.Function(
            self,
            "EndpointInstanceProbe",
            function_name="lachesis-endpoint-instance-probe",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(_INSTANCE_PROBE),
            timeout=cdk.Duration.seconds(60),
            memory_size=128,
        )
        probe.add_to_role_policy(
            iam.PolicyStatement(
                actions=["sagemaker:DescribeEndpoint"],
                resources=[
                    f"arn:aws:sagemaker:eu-west-1:{self.account}:endpoint/lachesis-decider"
                ],
            )
        )
        probe.add_to_role_policy(
            iam.PolicyStatement(actions=["cloudwatch:PutMetricData"], resources=["*"])
        )
        events.Rule(
            self,
            "EndpointInstanceProbeSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(5)),
            targets=[targets.LambdaFunction(probe)],
        )
        instance_count = cloudwatch.Metric(
            namespace="Lachesis/Decider",
            metric_name="EndpointInstanceCount",
            dimensions_map={"EndpointName": "lachesis-decider"},
            statistic="Maximum",
            period=cdk.Duration.minutes(5),
            unit=cloudwatch.Unit.COUNT,
        )
        instance_alarm = cloudwatch.Alarm(
            self,
            "EndpointInstanceUp",
            metric=instance_count,
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description=(
                "A Decider GPU instance is running (lachesis-decider CurrentInstanceCount >= 1). "
                "Expected while deliberately warm; otherwise scale to zero or delete the endpoint."
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        instance_alarm.add_alarm_action(cw_actions.SnsAction(foundation.alerts_topic))

        dashboard = cloudwatch.Dashboard(self, "LachesisDashboard", dashboard_name="lachesis")
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Decider ModelLatency (ms)",
                left=[latency],
            ),
            cloudwatch.GraphWidget(
                title="Decider no-capacity failures",
                left=[capacity],
            ),
            cloudwatch.GraphWidget(
                title="Bedrock throttles",
                left=[throttles],
            ),
        )

        cdk.CfnOutput(self, "DashboardName", value="lachesis")

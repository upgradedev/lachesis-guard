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
from constructs import Construct


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

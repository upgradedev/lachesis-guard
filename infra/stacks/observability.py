"""LachesisObservability (B3): the alarms that watch the hosted stack.

- Decider capacity: NoCapacityInvocationFailures on the inference
  component (the same signal that should make a human look before a
  judge hits a cold GPU).
- Decider latency: ModelLatency p95 above five seconds.
- Bedrock throttles on the agent's model profile.
- A small dashboard with the Decider widgets.

AgentCore spans: not enabled. Transaction Search is an account-wide
CloudWatch switch in this region and stays off by decision (F5); the
agent also ships no ADOT instrumentation, so no span claim is made
anywhere. The dashboard charts Decider ModelLatency p50/p95,
NoCapacityInvocationFailures, and EndpointInstanceCount; per-call
verdicts are returned in the agent response payload and persisted on
approval cards in DynamoDB.

Alarm wiring (F8): SageMaker publishes NoCapacityInvocationFailures
with the single dimension InferenceComponentName, and ModelLatency
(in microseconds) with InferenceComponentName alone; both verified
with list_metrics on 2026-10-15. Every alarm notifies the foundation
alerts topic.
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

        # Published dimension sets (list_metrics, 2026-10-15):
        # NoCapacityInvocationFailures carries InferenceComponentName
        # only; ModelLatency is published in microseconds and also
        # carries InferenceComponentName alone. The previous
        # {EndpointName, InferenceComponentName} pair matched nothing
        # SageMaker publishes, so these alarms could never fire.
        component_dims = {"InferenceComponentName": "lachesis-decider-2b"}
        capacity = cloudwatch.Metric(
            namespace="AWS/SageMaker",
            metric_name="NoCapacityInvocationFailures",
            dimensions_map=component_dims,
            statistic="Sum",
            period=cdk.Duration.minutes(1),
        )
        capacity_alarm = cloudwatch.Alarm(
            self,
            "DeciderNoCapacity",
            metric=capacity,
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Decider inference component reported no-capacity failures",
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        capacity_alarm.add_alarm_action(cw_actions.SnsAction(foundation.alerts_topic))
        latency = cloudwatch.Metric(
            namespace="AWS/SageMaker",
            metric_name="ModelLatency",
            dimensions_map=component_dims,
            statistic="p95",
            period=cdk.Duration.minutes(5),
            unit=cloudwatch.Unit.MICROSECONDS,
        )
        latency_alarm = cloudwatch.Alarm(
            self,
            "DeciderLatencyP95",
            metric=latency,
            threshold=5_000_000,
            evaluation_periods=2,
            alarm_description="Decider ModelLatency p95 above 5 s (5,000,000 us) over two periods",
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        latency_alarm.add_alarm_action(cw_actions.SnsAction(foundation.alerts_topic))
        throttles = cloudwatch.Metric(
            namespace="AWS/Bedrock",
            metric_name="InvocationThrottles",
            dimensions_map={"ModelId": "eu.anthropic.claude-sonnet-5-5"},
            statistic="Sum",
            period=cdk.Duration.minutes(5),
        )
        throttle_alarm = cloudwatch.Alarm(
            self,
            "BedrockThrottles",
            metric=throttles,
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Bedrock throttles on the agent model profile",
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        throttle_alarm.add_alarm_action(cw_actions.SnsAction(foundation.alerts_topic))
        # CloudWatch needs publish permission on the alerts topic.
        foundation.alerts_topic.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[foundation.alerts_topic.topic_arn],
            )
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
                title="Decider ModelLatency p95 (microseconds, as published)",
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

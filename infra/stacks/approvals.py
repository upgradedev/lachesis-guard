"""LachesisApprovals (B1) and the /try lane (B2).

HTTP API + Cognito (approvers group) + three Lambdas (api, runner,
sweeper) + DynamoDB + the console on S3/CloudFront. The runner is the
only principal that invokes the agent runtime; the API records a
decision with a conditional write (409 on a repeat) and async-invokes
the runner with retries disabled, so a duplicate event resumes once.
Paused calls appear in the console queue for the approver; an SES card is
optional and is sent only when the runner's SES addresses are set (the
hosted demo leaves them unset). GET never approves.

Each approval command is one DynamoDB TransactWriteItems (approvals/
lambda/approval/uow.py): the run row on lachesis-approvals plus its
domain events on lachesis-run-events. TransactWriteItems has no IAM
action of its own; each item is authorized as its PutItem or UpdateItem,
so each function is granted only the item actions its code issues.

lachesis-run-events is a transactional outbox (B2): its NEW_IMAGE
stream feeds the publisher Lambda (approvals/lambda/publisher.py), which
puts each new event on the custom bus lachesis-events with a whitelisted
detail. Records that still fail after the mapping's retries land on
lachesis-publisher-dlq; a RunFailed rule on the bus and the queue depth
each have an alarm on the foundation alerts topic.
"""

from __future__ import annotations

import os
import pathlib

import aws_cdk as cdk
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_sqs as sqs
from constructs import Construct

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
# What grant_read_write_data gave each function on the foundation CMK
# before B1, kept as is: the tables are encrypted with that key.
_KEY_ACTIONS = (
    "kms:Decrypt",
    "kms:DescribeKey",
    "kms:Encrypt",
    "kms:ReEncrypt*",
    "kms:GenerateDataKey*",
)
# The longest an async runner event may wait in the Lambda queue before
# it is dropped. runner.STARTING_TIMEOUT_S (1260 s) assumes this plus the
# 900 s Lambda limit: a start claimed later than this could be closed by
# the sweeper while it still runs. A dropped resume event is re-dispatched
# by the sweeper (runner.REDISPATCH_AFTER_S).
RUNNER_MAX_EVENT_AGE_S = 300
# The outbox relay (B2). The names are literal because the RunFailed
# alarm's metric dimensions must name the bus and the rule exactly.
EVENT_BUS_NAME = "lachesis-events"
EVENT_SOURCE = "lachesis.approvals"
RUN_FAILED_RULE_NAME = "lachesis-run-failed"
LAMBDA_ASSET = str(_REPO_ROOT / "approvals" / "lambda")
CONSOLE_ASSET = str(_REPO_ROOT / "approvals" / "console")


class ApprovalsStack(cdk.Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, foundation, agent, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        table = dynamodb.Table(
            self,
            "ApprovalsTable",
            table_name="lachesis-approvals",
            partition_key=dynamodb.Attribute(
                name="approval_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=foundation.key,
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        table.add_global_secondary_index(
            index_name="status-index",
            partition_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="created_ts", type=dynamodb.AttributeType.NUMBER
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # The append-only event log of each run (B1): pk approval_id, sk
        # the per-run seq, event_id = approval_id#seq. It is the outbox:
        # its stream feeds the publisher below (B2).
        events_table = dynamodb.Table(
            self,
            "RunEventsTable",
            table_name="lachesis-run-events",
            partition_key=dynamodb.Attribute(
                name="approval_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="seq", type=dynamodb.AttributeType.NUMBER),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=foundation.key,
            time_to_live_attribute="ttl",
            stream=dynamodb.StreamViewType.NEW_IMAGE,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        def grant_store(fn, approvals_actions):
            """Exactly the item actions a function issues: on the run
            table (Table.grant also covers its status-index) and PutItem
            on the event log, plus the CMK both tables are encrypted with
            (Table.grant does not grant the key)."""
            table.grant(fn, *(f"dynamodb:{action}" for action in approvals_actions))
            events_table.grant(fn, "dynamodb:PutItem")
            foundation.key.grant(fn, *_KEY_ACTIONS)

        pool = cognito.UserPool(
            self,
            "ApproversPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True),
            # F24: the approver is the only identity that can release a
            # governed write; optional TOTP can be enrolled at first
            # sign-in (not required, so enrolment never locks the owner
            # out before it is set up).
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        cognito.CfnUserPoolGroup(
            self,
            "ApproversGroup",
            user_pool_id=pool.user_pool_id,
            group_name="approvers",
            description="People who may approve a paused Lachesis call",
        )
        client = pool.add_client(
            "ConsoleClient",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            generate_secret=False,
        )

        code = lambda_.Code.from_asset(LAMBDA_ASSET)
        common_env = {
            "APPROVALS_TABLE": table.table_name,
            "RUN_EVENTS_TABLE": events_table.table_name,
        }

        runner = lambda_.Function(
            self,
            "Runner",
            function_name="lachesis-runner",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="runner.handler",
            code=code,
            timeout=cdk.Duration.minutes(15),
            memory_size=512,
            environment={
                **common_env,
                "AGENT_RUNTIME_ARN": agent.runtime_arn,
                "SES_FROM": os.environ.get("LACHESIS_SES_FROM", ""),
                "SES_TO": os.environ.get("LACHESIS_SES_TO", ""),
            },
        )
        # Claim, pause and outcome updates. PutItem is no longer issued by
        # this code (the API writes the STARTING row now); it stays for one
        # release so a runner still on the earlier code, which wrote the
        # row itself, does not fail with AccessDenied while CloudFormation
        # moves the role policy ahead of the function code. Drop it in the
        # release after this one. No Query: the sweep runs in the sweeper.
        grant_store(runner, ("GetItem", "PutItem", "UpdateItem"))
        runner.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[agent.runtime_arn, f"{agent.runtime_arn}/*"],
            )
        )
        runner.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail"],
                resources=[
                    f"arn:aws:ses:eu-west-1:{self.account}:identity/*",
                ],
            )
        )

        sweeper = lambda_.Function(
            self,
            "Sweeper",
            function_name="lachesis-sweeper",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="sweeper.handler",
            code=code,
            timeout=cdk.Duration.minutes(5),
            memory_size=256,
            environment={
                **common_env,
                "AGENT_RUNTIME_ARN": agent.runtime_arn,
                "RUNNER_ARN": runner.function_arn,
            },
        )
        # Reads the status-index, then updates the rows it closes.
        grant_store(sweeper, ("Query", "UpdateItem"))
        # Re-dispatches the resume of a decided run whose hand-off was lost.
        runner.grant_invoke(sweeper)
        sweeper.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[agent.runtime_arn, f"{agent.runtime_arn}/*"],
            )
        )
        events.Rule(
            self,
            "SweepSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(15)),
            targets=[targets.LambdaFunction(sweeper)],
        )

        api_fn = lambda_.Function(
            self,
            "Api",
            function_name="lachesis-approvals-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="api.handler",
            code=code,
            timeout=cdk.Duration.seconds(29),
            memory_size=256,
            environment={
                **common_env,
                "RUNNER_ARN": runner.function_arn,
                "TRY_DAILY_CAP": "100",
                "TRY_IP_DAILY_CAP": "5",
            },
        )
        # Puts new run rows, updates counters and decisions, reads rows
        # and the approver queue.
        grant_store(api_fn, ("GetItem", "PutItem", "UpdateItem", "Query"))
        runner.grant_invoke(api_fn)
        lambda_.CfnEventInvokeConfig(
            self,
            "RunnerInvokeConfig",
            function_name=runner.function_name,
            qualifier="$LATEST",
            maximum_retry_attempts=0,
            maximum_event_age_in_seconds=RUNNER_MAX_EVENT_AGE_S,
        )

        api = apigw.CfnApi(
            self,
            "ApprovalsApi",
            name="lachesis-approvals",
            protocol_type="HTTP",
            cors_configuration=apigw.CfnApi.CorsProperty(
                allow_origins=["*"],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["authorization", "content-type"],
            ),
        )
        authorizer = apigw.CfnAuthorizer(
            self,
            "JwtAuthorizer",
            api_id=api.attr_api_id,
            authorizer_type="JWT",
            name="cognito-approvers",
            identity_source=["$request.header.Authorization"],
            jwt_configuration=apigw.CfnAuthorizer.JWTConfigurationProperty(
                issuer=f"https://cognito-idp.eu-west-1.amazonaws.com/{pool.user_pool_id}",
                audience=[client.user_pool_client_id],
            ),
        )
        integration = apigw.CfnIntegration(
            self,
            "ApiIntegration",
            api_id=api.attr_api_id,
            integration_type="AWS_PROXY",
            integration_uri=api_fn.function_arn,
            payload_format_version="2.0",
        )
        routes = (
            ("GET /approvals", "JWT"),
            ("GET /approvals/{id}", "JWT"),
            ("POST /approvals/{id}/decision", "JWT"),
            ("POST /approvals/start", "JWT"),
            ("POST /try", "NONE"),
            ("GET /try/{token}", "NONE"),
            ("POST /try/{token}/decision", "NONE"),
        )
        route_resources = []
        for route_key, auth_type in routes:
            slug = "".join(
                part.title()
                for part in route_key.replace("/", " ")
                .replace("{", "")
                .replace("}", "")
                .split()
            )
            route = apigw.CfnRoute(
                self,
                "Route" + slug,
                api_id=api.attr_api_id,
                route_key=route_key,
                target=f"integrations/{integration.attr_integration_id}",
                authorization_type=auth_type,
                authorizer_id=authorizer.attr_authorizer_id if auth_type == "JWT" else None,
            )
            route.node.add_dependency(integration)
            route_resources.append(route)
        stage = apigw.CfnStage(
            self,
            "DefaultStage",
            api_id=api.attr_api_id,
            stage_name="$default",
            auto_deploy=True,
        )
        # Route throttle for POST /try (F7). Written as a raw override:
        # CDK's RouteSettingsProperty serializes throttlingBurstLimit
        # with a lowercase t, which ApiGatewayV2 rejects ("Unrecognized
        # field", deploy of 2026-10-15); the service wants
        # ThrottlingBurstLimit / ThrottlingRateLimit verbatim.
        stage.add_property_override(
            "RouteSettings",
            {"POST /try": {"ThrottlingRateLimit": 0.2, "ThrottlingBurstLimit": 2}},
        )
        # ApiGateway validates RouteSettings against existing routes at
        # stage creation ("Unable to find Route by key POST /try", deploy
        # of 2026-10-15), so the stage waits for every route.
        for route_resource in route_resources:
            stage.node.add_dependency(route_resource)
        lambda_.CfnPermission(
            self,
            "ApiInvokePermission",
            action="lambda:InvokeFunction",
            function_name=api_fn.function_name,
            principal="apigateway.amazonaws.com",
            source_arn=f"arn:aws:execute-api:eu-west-1:{self.account}:{api.attr_api_id}/*",
        )

        console_bucket = s3.Bucket(
            self,
            "ConsoleBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=foundation.key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        headers_policy = cloudfront.ResponseHeadersPolicy(
            self,
            "ConsoleHeadersPolicy",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.SAME_ORIGIN,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=cdk.Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
            ),
            remove_headers=[
                "x-amz-server-side-encryption",
                "x-amz-server-side-encryption-aws-kms-key-id",
            ],
        )
        distribution = cloudfront.Distribution(
            self,
            "ConsoleDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(console_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=headers_policy,
            ),
        )
        config_js = (
            f'window.LACHESIS_CONFIG = {{"apiUrl": "{api.attr_api_endpoint}", '
            f'"clientId": "{client.user_pool_client_id}", '
            f'"poolId": "{pool.user_pool_id}", "region": "eu-west-1", '
            # The commit the deploy ran from, shown in the console footer.
            f'"build": "{os.environ.get("GITHUB_SHA", "")[:7]}"}};'
        )
        s3deploy.BucketDeployment(
            self,
            "ConsoleDeploy",
            sources=[
                s3deploy.Source.asset(CONSOLE_ASSET),
                s3deploy.Source.data("config.js", config_js),
            ],
            destination_bucket=console_bucket,
            distribution=distribution,
        )
        runner.add_environment("CONSOLE_URL", f"https://{distribution.domain_name}")

        # --- B2: the outbox relay to EventBridge -----------------------
        # The custom bus, with the AWS owned key (no kms_key given).
        bus = events.EventBus(self, "EventsBus", event_bus_name=EVENT_BUS_NAME)
        # Where a stream batch goes once its retries are spent. Lambda
        # sends the shard and sequence range, not the record, so a redrive
        # re-reads lachesis-run-events. SSE-SQS: no new CMK.
        publisher_dlq = sqs.Queue(
            self,
            "PublisherDlq",
            queue_name="lachesis-publisher-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=cdk.Duration.days(14),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        publisher = lambda_.Function(
            self,
            "Publisher",
            function_name="lachesis-run-events-publisher",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="publisher.handler",
            code=code,
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment={"EVENT_BUS_NAME": bus.event_bus_name},
        )
        # TRIM_HORIZON: a mapping created or re-created later still relays
        # every row the stream holds, so the outbox never skips an event.
        # The first deploy therefore also relays up to 24 h of rows already
        # written (the stream's retention), and the RunFailed alarm may
        # fire for a failure from that window. Consumers dedupe on
        # event_id, so a replayed event is harmless to them.
        publisher.add_event_source(
            event_sources.DynamoEventSource(
                events_table,
                starting_position=lambda_.StartingPosition.TRIM_HORIZON,
                batch_size=10,
                bisect_batch_on_error=True,
                retry_attempts=3,
                report_batch_item_failures=True,
                filters=[
                    lambda_.FilterCriteria.filter(
                        {"eventName": lambda_.FilterRule.is_equal("INSERT")}
                    )
                ],
                on_failure=event_sources.SqsDlq(publisher_dlq),
            )
        )
        # Stated explicitly rather than left to the event source's own
        # grants: the on-failure send to the queue, the CMK the stream's
        # table is encrypted with, and PutEvents on this bus only. The
        # publisher has no table item actions.
        publisher.add_to_role_policy(
            iam.PolicyStatement(actions=["sqs:SendMessage"], resources=[publisher_dlq.queue_arn])
        )
        foundation.key.grant(publisher, "kms:Decrypt")
        publisher.add_to_role_policy(
            iam.PolicyStatement(actions=["events:PutEvents"], resources=[bus.event_bus_arn])
        )

        # A failed run is an alarm, not a rule target: CloudWatch alarm
        # actions on the existing alerts topic (whose CloudWatch publish
        # statement ObservabilityStack adds), so this stack never touches
        # the Foundation topic policy.
        events.Rule(
            self,
            "RunFailedRule",
            rule_name=RUN_FAILED_RULE_NAME,
            event_bus=bus,
            description="A Lachesis run ended in ERROR (RunFailed from the outbox)",
            event_pattern=events.EventPattern(source=[EVENT_SOURCE], detail_type=["RunFailed"]),
        )
        # Not verified on AWS yet: that MatchedEvents for a rule on a
        # custom bus is published with EventBusName and RuleName, and that
        # a rule with no targets publishes it at all. Check with
        # list_metrics after the first RunFailed, as was done for the
        # SageMaker alarms in observability.py (F8).
        run_failed = cloudwatch.Metric(
            namespace="AWS/Events",
            metric_name="MatchedEvents",
            dimensions_map={
                "EventBusName": EVENT_BUS_NAME,
                "RuleName": RUN_FAILED_RULE_NAME,
            },
            statistic="Sum",
            period=cdk.Duration.minutes(5),
        )
        run_failed_alarm = cloudwatch.Alarm(
            self,
            "RunFailedAlarm",
            metric=run_failed,
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="A Lachesis run ended in ERROR (RunFailed on lachesis-events)",
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        run_failed_alarm.add_alarm_action(cw_actions.SnsAction(foundation.alerts_topic))
        dlq_alarm = cloudwatch.Alarm(
            self,
            "PublisherDlqAlarm",
            metric=publisher_dlq.metric_approximate_number_of_messages_visible(
                period=cdk.Duration.minutes(5), statistic="Maximum"
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="The outbox publisher gave up on a stream batch (publisher DLQ)",
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        dlq_alarm.add_alarm_action(cw_actions.SnsAction(foundation.alerts_topic))

        self.events_table = events_table
        self.event_bus = bus
        self.publisher_dlq = publisher_dlq
        self.api_url = api.attr_api_endpoint
        self.console_url = f"https://{distribution.domain_name}"
        self.user_pool = pool
        self.user_pool_client = client

        cdk.CfnOutput(self, "ApiUrl", value=self.api_url)
        cdk.CfnOutput(self, "ConsoleUrl", value=self.console_url)
        cdk.CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        cdk.CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)

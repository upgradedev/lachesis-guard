"""LachesisApprovals (B1) and the /try lane (B2).

HTTP API + Cognito (approvers group) + three Lambdas (api, runner,
sweeper) + DynamoDB + the console on S3/CloudFront. The runner is the
only principal that invokes the agent runtime; the API records a
decision with a conditional write (409 on a repeat) and async-invokes
the runner with retries disabled, so a duplicate event resumes once.
SES sends the approval card; GET never approves.
"""

from __future__ import annotations

import pathlib

import aws_cdk as cdk
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
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
        common_env = {"APPROVALS_TABLE": table.table_name}

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
                "SES_FROM": "info@upgrade.net.gr",
                "SES_TO": "efousekis@icloud.com",
            },
        )
        table.grant_read_write_data(runner)
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
                    f"arn:aws:ses:eu-west-1:{self.account}:identity/info@upgrade.net.gr",
                    f"arn:aws:ses:eu-west-1:{self.account}:identity/efousekis@icloud.com",
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
            environment={**common_env, "AGENT_RUNTIME_ARN": agent.runtime_arn},
        )
        table.grant_read_write_data(sweeper)
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
        table.grant_read_write_data(api_fn)
        runner.grant_invoke(api_fn)
        lambda_.CfnEventInvokeConfig(
            self,
            "RunnerInvokeConfig",
            function_name=runner.function_name,
            qualifier="$LATEST",
            maximum_retry_attempts=0,
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
        )
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
        apigw.CfnStage(
            self,
            "DefaultStage",
            api_id=api.attr_api_id,
            stage_name="$default",
            auto_deploy=True,
            route_settings={
                "POST /try": apigw.CfnStage.RouteSettingsProperty(
                    throttling_rate_limit=0.2,
                    throttling_burst_limit=2,
                )
            },
        )
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
        distribution = cloudfront.Distribution(
            self,
            "ConsoleDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(console_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
        )
        config_js = (
            f'window.LACHESIS_CONFIG = {{"apiUrl": "{api.attr_api_endpoint}", '
            f'"clientId": "{client.user_pool_client_id}", '
            f'"poolId": "{pool.user_pool_id}", "region": "eu-west-1"}};'
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

        self.api_url = api.attr_api_endpoint
        self.console_url = f"https://{distribution.domain_name}"
        self.user_pool = pool
        self.user_pool_client = client

        cdk.CfnOutput(self, "ApiUrl", value=self.api_url)
        cdk.CfnOutput(self, "ConsoleUrl", value=self.console_url)
        cdk.CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        cdk.CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)

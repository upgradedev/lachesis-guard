"""LachesisFoundation: shared, cheap, always-on resources.

KMS CMK, the S3 bucket that holds AgentCore session state, the two ECR
repositories (Decider BYOC image, agent image), the CodeBuild projects
that build them, and the build budget alarm. Nothing in this stack
bills while idle beyond storage cents.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    aws_budgets as budgets,
)
from aws_cdk import (
    aws_codebuild as codebuild,
)
from aws_cdk import (
    aws_ecr as ecr,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    aws_sns_subscriptions as subscriptions,
)
from constructs import Construct

BUILD_BUDGET_USD = 230


class FoundationStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.key = kms.Key(
            self,
            "LachesisKey",
            alias="alias/lachesis-guard",
            enable_key_rotation=True,
            description="Lachesis CMK for sessions, approvals and logs",
        )

        self.sessions_bucket = s3.Bucket(
            self,
            "SessionsBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.key,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        self.source_bucket = s3.Bucket(
            self,
            "SourceBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.key,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        self.decider_repo = ecr.Repository(
            self,
            "DeciderRepo",
            repository_name="lachesis-decider",
            image_scan_on_push=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )
        self.agent_repo = ecr.Repository(
            self,
            "AgentRepo",
            repository_name="lachesis-agent",
            image_scan_on_push=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )

        self.decider_build = codebuild.Project(
            self,
            "DeciderImageBuild",
            project_name="lachesis-decider-image",
            source=codebuild.Source.s3(
                bucket=self.source_bucket,
                path="decider-image-source.zip",
            ),
            build_spec=codebuild.BuildSpec.from_source_filename("decider-image/buildspec.yml"),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                privileged=True,
                compute_type=codebuild.ComputeType.LARGE,
            ),
            environment_variables={
                "AWS_DEFAULT_REGION": codebuild.BuildEnvironmentVariable(value="eu-west-1"),
                "IMAGE_REPO": codebuild.BuildEnvironmentVariable(
                    value=self.decider_repo.repository_name
                ),
            },
        )
        self.decider_repo.grant_pull_push(self.decider_build)

        self.agent_build = codebuild.Project(
            self,
            "AgentImageBuild",
            project_name="lachesis-agent-image",
            source=codebuild.Source.s3(
                bucket=self.source_bucket,
                path="agent-image-source.zip",
            ),
            build_spec=codebuild.BuildSpec.from_source_filename("agent/buildspec.yml"),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                privileged=True,
                compute_type=codebuild.ComputeType.LARGE,
            ),
            environment_variables={
                "AWS_DEFAULT_REGION": codebuild.BuildEnvironmentVariable(value="eu-west-1"),
                "IMAGE_REPO": codebuild.BuildEnvironmentVariable(
                    value=self.agent_repo.repository_name
                ),
            },
        )
        self.agent_repo.grant_pull_push(self.agent_build)

        alerts = sns.Topic(self, "BudgetAlerts", topic_name="lachesis-budget-alerts")
        self.alerts_topic = alerts
        # AWS Budgets publishes to the topic; without this policy the
        # notifications are dropped (F4: the topic had 0 subscribers and
        # no publish permission, so the guard never fired).
        alerts.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[alerts.topic_arn],
            )
        )
        # The owner's address is supplied at deploy time
        # (cdk deploy -c alert_email=<address>); it is not hard-coded
        # in the public repo (F18). The owner confirms the subscription
        # email. No address, no subscription.
        alert_email = self.node.try_get_context("alert_email")
        if alert_email:
            alerts.add_subscription(subscriptions.EmailSubscription(alert_email))
        budgets.CfnBudget(
            self,
            "BuildBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="lachesis-build",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=BUILD_BUDGET_USD, unit="USD"),
                # Only Lachesis-tagged spend counts. Unfiltered, this
                # budget tracked the whole account and sat in ALARM from
                # day one (F4). Tag activation for app=lachesis-guard is
                # already Active in this account.
                cost_filters={"TagKeyValue": ["app$lachesis-guard"]},
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="ACTUAL",
                        comparison_operator="GREATER_THAN",
                        threshold=50,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="SNS", address=alerts.topic_arn
                        )
                    ],
                ),
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="FORECASTED",
                        comparison_operator="GREATER_THAN",
                        threshold=100,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="SNS", address=alerts.topic_arn
                        )
                    ],
                ),
            ],
        )

        cdk.CfnOutput(self, "SessionsBucketName", value=self.sessions_bucket.bucket_name)
        cdk.CfnOutput(self, "SourceBucketName", value=self.source_bucket.bucket_name)
        cdk.CfnOutput(self, "DeciderRepoUri", value=self.decider_repo.repository_uri)
        cdk.CfnOutput(self, "AgentRepoUri", value=self.agent_repo.repository_uri)
        cdk.CfnOutput(self, "KmsKeyArn", value=self.key.key_arn)

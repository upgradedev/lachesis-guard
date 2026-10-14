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
    aws_sns as sns,
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
        budgets.CfnBudget(
            self,
            "BuildBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="lachesis-build",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=BUILD_BUDGET_USD, unit="USD"),
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
        cdk.CfnOutput(self, "DeciderRepoUri", value=self.decider_repo.repository_uri)
        cdk.CfnOutput(self, "AgentRepoUri", value=self.agent_repo.repository_uri)
        cdk.CfnOutput(self, "KmsKeyArn", value=self.key.key_arn)

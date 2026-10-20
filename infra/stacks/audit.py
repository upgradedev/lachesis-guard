"""LachesisAudit (B3): the Audit and Insight bounded contexts.

Audit keeps every event the outbox publisher puts on lachesis-events:

- the rule audit-all on that bus (source lachesis.approvals) sends each
  event's ``detail`` to the Amazon Data Firehose stream lachesis-audit,
  with retries and the dead-letter queue lachesis-audit-dlq (SSE-SQS,
  sendable only by this rule);
- Firehose appends a newline to each record and writes JSON lines to the
  audit bucket under events/dt=YYYY-MM-DD/ (failed records under
  errors/), buffering 60 s or 5 MB;
- the bucket blocks all public access, enforces TLS, encrypts with SSE-S3
  (no new CMK, and no dependency on the Foundation key), is versioned,
  and expires events/ and errors/ after 90 days and athena-results/
  after 7.

Insight reads it: the Glue database lachesis_audit with the table events
(OpenX JSON SerDe, partition projection on dt, no crawler), the Athena
workgroup lachesis (configuration enforced, results under
athena-results/, 1 GB scan cutoff per query) and four named queries. The
schema and the SQL live in stacks/audit_schema.py, which has no CDK
import so the offline tests can pin them against the publisher.

The stack depends on no other stack: it names the bus by its fixed name
and builds the ARNs it needs from its own account and region, so there
is no cross-stack export. The bus belongs to LachesisApprovals, so this
stack deploys after it and is destroyed before it (iac.yml).

None of this has run on AWS yet. In particular the AppendDelimiterToRecord
processor (the console's "New line delimiter" option for an S3
destination) and the queries are unverified until the first deploy.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_athena as athena
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kinesisfirehose as firehose
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from . import audit_schema as schema

# The Foundation alerts topic, named rather than imported (no export).
ALERTS_TOPIC_NAME = "lachesis-budget-alerts"
# How long EventBridge keeps retrying a delivery to Firehose before the
# event goes to the dead-letter queue.
TARGET_MAX_EVENT_AGE_S = 3600
TARGET_MAX_RETRIES = 20
DELIVERY_LOG_GROUP = f"/aws/kinesisfirehose/{schema.DELIVERY_STREAM_NAME}"
DELIVERY_LOG_STREAM = "S3Delivery"

_FH = firehose.CfnDeliveryStream


def _query_id(name: str) -> str:
    """A stable logical id from the query name (lachesis-time-to-decision
    becomes InsightTimeToDecision), so reordering QUERIES replaces nothing."""
    words = name.removeprefix("lachesis-").split("-")
    return "Insight" + "".join(word.capitalize() for word in words)


class AuditStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- the lake ---------------------------------------------------
        bucket = s3.Bucket(
            self,
            "AuditBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="events",
                    prefix=schema.EVENTS_PREFIX,
                    expiration=cdk.Duration.days(schema.EVENTS_EXPIRY_DAYS),
                    noncurrent_version_expiration=cdk.Duration.days(1),
                    abort_incomplete_multipart_upload_after=cdk.Duration.days(1),
                ),
                s3.LifecycleRule(
                    id="errors",
                    prefix=schema.ERRORS_PREFIX,
                    expiration=cdk.Duration.days(schema.ERRORS_EXPIRY_DAYS),
                    noncurrent_version_expiration=cdk.Duration.days(1),
                    abort_incomplete_multipart_upload_after=cdk.Duration.days(1),
                ),
                s3.LifecycleRule(
                    id="athena-results",
                    prefix=schema.RESULTS_PREFIX,
                    expiration=cdk.Duration.days(schema.RESULTS_EXPIRY_DAYS),
                    noncurrent_version_expiration=cdk.Duration.days(1),
                    abort_incomplete_multipart_upload_after=cdk.Duration.days(1),
                ),
            ],
            # Like every Lachesis stack: the iac destroy action removes it,
            # objects and versions included.
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # --- delivery: Firehose ----------------------------------------
        delivery_logs = logs.LogGroup(
            self,
            "AuditDeliveryLogs",
            log_group_name=DELIVERY_LOG_GROUP,
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        delivery_log_stream = logs.LogStream(
            self,
            "AuditDeliveryLogStream",
            log_group=delivery_logs,
            log_stream_name=DELIVERY_LOG_STREAM,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        # The Firehose role may write only under events/ and errors/ of
        # this bucket and log only to its own log stream. No read of
        # objects, no delete, no other bucket.
        delivery_role = iam.Role(
            self,
            "AuditDeliveryRole",
            assumed_by=iam.ServicePrincipal(
                "firehose.amazonaws.com",
                conditions={"StringEquals": {"sts:ExternalId": self.account}},
            ),
            description="Firehose lachesis-audit: write audit lines to the audit bucket only",
        )
        delivery_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:AbortMultipartUpload"],
                resources=[
                    bucket.arn_for_objects(f"{schema.EVENTS_PREFIX}*"),
                    bucket.arn_for_objects(f"{schema.ERRORS_PREFIX}*"),
                ],
            )
        )
        # The bucket-level actions of the documented Firehose S3 policy,
        # on this bucket only. The object read that policy also lists
        # is left out: delivery only writes.
        delivery_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:GetBucketLocation",
                    "s3:ListBucket",
                    "s3:ListBucketMultipartUploads",
                ],
                resources=[bucket.bucket_arn],
            )
        )
        delivery_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:PutLogEvents"],
                resources=[
                    (
                        f"arn:{self.partition}:logs:{self.region}:{self.account}:log-group:"
                        f"{DELIVERY_LOG_GROUP}:log-stream:{DELIVERY_LOG_STREAM}"
                    )
                ],
            )
        )

        stream = _FH(
            self,
            "AuditDeliveryStream",
            delivery_stream_name=schema.DELIVERY_STREAM_NAME,
            # EventBridge calls PutRecord on the stream: a DirectPut
            # stream with no producer of our own.
            delivery_stream_type="DirectPut",
            delivery_stream_encryption_configuration_input=(
                _FH.DeliveryStreamEncryptionConfigurationInputProperty(key_type="AWS_OWNED_CMK")
            ),
            extended_s3_destination_configuration=_FH.ExtendedS3DestinationConfigurationProperty(
                bucket_arn=bucket.bucket_arn,
                role_arn=delivery_role.role_arn,
                prefix=schema.DATA_OUTPUT_PREFIX,
                error_output_prefix=schema.ERROR_OUTPUT_PREFIX,
                buffering_hints=_FH.BufferingHintsProperty(
                    interval_in_seconds=schema.BUFFER_SECONDS,
                    size_in_m_bs=schema.BUFFER_MB,
                ),
                compression_format="UNCOMPRESSED",
                # The bucket's default SSE-S3 encrypts every object.
                encryption_configuration=_FH.EncryptionConfigurationProperty(
                    no_encryption_config="NoEncryption"
                ),
                # One JSON object per line, which the JSON SerDe needs.
                processing_configuration=_FH.ProcessingConfigurationProperty(
                    enabled=True,
                    processors=[
                        _FH.ProcessorProperty(
                            type="AppendDelimiterToRecord",
                            parameters=[
                                _FH.ProcessorParameterProperty(
                                    parameter_name="Delimiter", parameter_value=schema.DELIMITER
                                )
                            ],
                        )
                    ],
                ),
                cloud_watch_logging_options=_FH.CloudWatchLoggingOptionsProperty(
                    enabled=True,
                    log_group_name=DELIVERY_LOG_GROUP,
                    log_stream_name=DELIVERY_LOG_STREAM,
                ),
            ),
        )
        stream.node.add_dependency(delivery_logs, delivery_log_stream)
        # Firehose checks the role when the stream is created.
        stream.node.add_dependency(delivery_role)

        # --- routing: the rule, its role and its dead-letter queue ------
        dlq = sqs.Queue(
            self,
            "AuditDlq",
            queue_name=schema.DLQ_NAME,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=cdk.Duration.days(14),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        target_role = iam.Role(
            self,
            "AuditTargetRole",
            assumed_by=iam.ServicePrincipal(
                "events.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="EventBridge rule audit-all: put records on lachesis-audit only",
        )
        target_role.add_to_policy(
            iam.PolicyStatement(
                actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
                resources=[stream.attr_arn],
            )
        )
        rule = events.CfnRule(
            self,
            "AuditAllRule",
            name=schema.RULE_NAME,
            event_bus_name=schema.EVENT_BUS_NAME,
            description="Every Lachesis approvals event, to the audit lake",
            event_pattern={"source": [schema.EVENT_SOURCE]},
            state="ENABLED",
            targets=[
                events.CfnRule.TargetProperty(
                    id="AuditFirehose",
                    arn=stream.attr_arn,
                    role_arn=target_role.role_arn,
                    # Only the publisher's whitelisted detail reaches the
                    # lake: a line is one detail object.
                    input_path="$.detail",
                    retry_policy=events.CfnRule.RetryPolicyProperty(
                        maximum_event_age_in_seconds=TARGET_MAX_EVENT_AGE_S,
                        maximum_retry_attempts=TARGET_MAX_RETRIES,
                    ),
                    dead_letter_config=events.CfnRule.DeadLetterConfigProperty(arn=dlq.queue_arn),
                )
            ],
        )
        # The explicit grant: only this rule may send to the queue.
        dlq.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal("events.amazonaws.com")],
                actions=["sqs:SendMessage"],
                resources=[dlq.queue_arn],
                conditions={"ArnEquals": {"aws:SourceArn": rule.attr_arn}},
            )
        )
        rule.node.add_dependency(target_role)

        alerts = sns.Topic.from_topic_arn(
            self,
            "AlertsTopic",
            f"arn:{self.partition}:sns:{self.region}:{self.account}:{ALERTS_TOPIC_NAME}",
        )
        dlq_alarm = cloudwatch.Alarm(
            self,
            "AuditDlqSentAlarm",
            metric=dlq.metric_number_of_messages_sent(
                period=cdk.Duration.minutes(5), statistic="Sum"
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="An audit event could not be delivered to Firehose (audit DLQ)",
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        dlq_alarm.add_alarm_action(cw_actions.SnsAction(alerts))

        # --- Insight: Glue and Athena ----------------------------------
        database = glue.CfnDatabase(
            self,
            "AuditDatabase",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=schema.DATABASE,
                description="Lachesis audit lake: one row per event on lachesis-events",
            ),
        )
        table = glue.CfnTable(
            self,
            "AuditEventsTable",
            catalog_id=self.account,
            database_name=schema.DATABASE,
            table_input=glue.CfnTable.TableInputProperty(
                name=schema.TABLE,
                table_type="EXTERNAL_TABLE",
                description="The publisher's whitelisted detail, one JSON line per event",
                parameters=schema.table_parameters(bucket.bucket_name),
                partition_keys=[
                    glue.CfnTable.ColumnProperty(
                        name=schema.PARTITION_KEY[0], type=schema.PARTITION_KEY[1]
                    )
                ],
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{bucket.bucket_name}/{schema.EVENTS_PREFIX}",
                    input_format=schema.INPUT_FORMAT,
                    output_format=schema.OUTPUT_FORMAT,
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library=schema.SERDE,
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name=name, type=kind)
                        for name, kind in schema.COLUMNS
                    ],
                ),
            ),
        )
        table.add_dependency(database)

        workgroup = athena.CfnWorkGroup(
            self,
            "AuditWorkGroup",
            name=schema.WORKGROUP,
            description="Lachesis Insight queries over the audit lake",
            state="ENABLED",
            # The named queries go with the workgroup on a destroy.
            recursive_delete_option=True,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                bytes_scanned_cutoff_per_query=schema.BYTES_SCANNED_CUTOFF,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{bucket.bucket_name}/{schema.RESULTS_PREFIX}",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_S3"
                    ),
                ),
            ),
        )
        for name, (description, sql) in schema.QUERIES.items():
            query = athena.CfnNamedQuery(
                self,
                _query_id(name),
                name=name,
                description=description,
                database=schema.DATABASE,
                query_string=sql,
                work_group=workgroup.name,
            )
            query.add_dependency(workgroup)
            query.add_dependency(table)

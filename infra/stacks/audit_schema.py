"""The schema and the Insight queries of the audit lake (B3), as plain data.

No CDK import, so the offline tests can load this file on its own and
pin it against the publisher (approvals/lambda/publisher.py) without a
synth. infra/stacks/audit.py builds the Glue table and the Athena named
queries from these values.

The lake holds one JSON line per event the publisher put on
lachesis-events: the rule target sends only ``$.detail``, so a line is
exactly the publisher's whitelisted detail, and the Glue columns are
exactly publisher.DETAIL_FIELDS. The partition dt is the day Firehose
received the record (UTC), not a field of the event.

Delivery is at least once (the publisher re-sends, EventBridge and
Firehose retry), so the same event can sit in the lake more than once,
even on two different dt days. Every query therefore reads the events
through one deduplicating CTE (a re-sent detail is byte-identical, so
SELECT DISTINCT over the detail columns collapses the copies) and counts
with count(DISTINCT event_id). Days are the event's own ``at`` date, so
a copy that arrived after midnight does not count twice.

What the queries can and cannot say:

- Only paused tool calls reach the bus. Lachesis pauses only on a
  Confirm verdict (Proceed, Guide and Deny never pause a run), so a
  RunPaused row is a Confirm, and its layer says whether Decider,
  a deterministic layer, or nobody (``unavailable``, fail-closed) made
  it. There is no verdict column, and no query invents one.
- The decision on a pause is the status a DecisionRecorded row carries
  (APPROVED or DECLINED, the run status the command left).
- A pause and its decision pair on run_ref plus pause_seq. run_ref is
  the first 32 hex digits of the SHA-256 of the approval_id; the raw id
  is never on the bus or in the lake.
- lane and pause_seq reached the bus in B3. Rows relayed before that
  have neither, and the queries that need them leave those rows out
  instead of guessing (a missing lane is never read as the approval
  lane).
- The queries return counts with n and dates, and durations in seconds.
  They return no accuracy, rate of agreement or calibration figure.

Known limit: a resumed call re-issues its cached Confirm row with no new
model call. The bus carries no flag for that row, so a re-pause whose
card was built from it cannot be told apart here.
"""

from __future__ import annotations

EVENT_BUS_NAME = "lachesis-events"
EVENT_SOURCE = "lachesis.approvals"
RULE_NAME = "audit-all"
DELIVERY_STREAM_NAME = "lachesis-audit"
DLQ_NAME = "lachesis-audit-dlq"
DATABASE = "lachesis_audit"
TABLE = "events"
WORKGROUP = "lachesis"

EVENTS_PREFIX = "events/"
ERRORS_PREFIX = "errors/"
RESULTS_PREFIX = "athena-results/"
# Firehose expressions, not Python formatting. An ErrorOutputPrefix must
# hold !{firehose:error-output-type} once the Prefix uses an expression.
DATA_OUTPUT_PREFIX = EVENTS_PREFIX + "dt=!{timestamp:yyyy-MM-dd}/"
ERROR_OUTPUT_PREFIX = ERRORS_PREFIX + "!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/"
# The AppendDelimiterToRecord parameter: the two characters backslash
# and n, which Firehose reads as a newline after each record.
DELIMITER = "\\n"
BUFFER_SECONDS = 60
BUFFER_MB = 5
EVENTS_EXPIRY_DAYS = 90
ERRORS_EXPIRY_DAYS = 90
RESULTS_EXPIRY_DAYS = 7
# The per-query scan cutoff of the workgroup: 1 GB.
BYTES_SCANNED_CUTOFF = 1024**3
# How far back every query looks (partition projection prunes on dt).
LOOKBACK_DAYS = 30

# (name, Glue type), in publisher.DETAIL_FIELDS order. A test pins both.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("event_id", "string"),
    ("run_ref", "string"),
    ("seq", "bigint"),
    ("type", "string"),
    ("at", "string"),
    ("status", "string"),
    ("lane", "string"),
    ("pause_seq", "bigint"),
    ("tool", "string"),
    ("layer", "string"),
    ("risk", "string"),
    ("confidence", "double"),
    ("latency_ms", "double"),
)
PARTITION_KEY = ("dt", "string")
# Partition projection: the first day the lake can hold, then every day
# up to today. No crawler and no MSCK REPAIR.
PROJECTION_START = "2026-10-20"

SERDE = "org.openx.data.jsonserde.JsonSerDe"
INPUT_FORMAT = "org.apache.hadoop.mapred.TextInputFormat"
OUTPUT_FORMAT = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"


def table_parameters(bucket_name: str) -> dict[str, str]:
    """The Glue table parameters that turn on partition projection."""
    return {
        "classification": "json",
        "projection.enabled": "true",
        "projection.dt.type": "date",
        "projection.dt.format": "yyyy-MM-dd",
        "projection.dt.range": f"{PROJECTION_START},NOW",
        "projection.dt.interval": "1",
        "projection.dt.interval.unit": "DAYS",
        "storage.location.template": f"s3://{bucket_name}/{EVENTS_PREFIX}dt=${{dt}}/",
    }


def _column_list() -> str:
    return ", ".join(f'"{name}"' if name == "type" else name for name, _ in COLUMNS)


# The one way every query reads the lake: the last LOOKBACK_DAYS of dt
# partitions, with the at-least-once copies collapsed.
DEDUPED = (
    "ev AS (\n"
    f"  SELECT DISTINCT {_column_list()}\n"
    f"  FROM {DATABASE}.{TABLE}\n"
    f"  WHERE dt >= date_format(current_date - INTERVAL '{LOOKBACK_DAYS}' DAY, '%Y-%m-%d')\n"
    ")"
)

_PAUSE_MIX = f"""WITH {DEDUPED}
SELECT substr(at, 1, 10) AS day,
       layer,
       risk,
       count(DISTINCT event_id) AS n
FROM ev
WHERE "type" = 'RunPaused'
GROUP BY substr(at, 1, 10), layer, risk
ORDER BY day, layer, risk"""

_FAIL_CLOSED = f"""WITH {DEDUPED}
SELECT substr(at, 1, 10) AS day,
       count(DISTINCT event_id) AS n_paused,
       count(DISTINCT CASE WHEN layer = 'unavailable' THEN event_id END) AS n_fail_closed
FROM ev
WHERE "type" = 'RunPaused'
GROUP BY substr(at, 1, 10)
ORDER BY day"""

_TIME_TO_DECISION = f"""WITH {DEDUPED},
requested AS (
  SELECT DISTINCT run_ref, lane FROM ev WHERE "type" = 'RunRequested'
),
paused AS (
  SELECT run_ref, pause_seq, min(at) AS paused_at
  FROM ev
  WHERE "type" = 'RunPaused' AND pause_seq IS NOT NULL
  GROUP BY run_ref, pause_seq
),
decided AS (
  SELECT event_id, run_ref, pause_seq, at AS decided_at
  FROM ev
  WHERE "type" = 'DecisionRecorded' AND pause_seq IS NOT NULL
),
waits AS (
  SELECT d.event_id AS event_id,
         substr(d.decided_at, 1, 10) AS day,
         coalesce(r.lane, 'unknown') AS lane,
         date_diff('second', from_iso8601_timestamp(p.paused_at),
                   from_iso8601_timestamp(d.decided_at)) AS wait_seconds
  FROM decided AS d
  JOIN paused AS p ON p.run_ref = d.run_ref AND p.pause_seq = d.pause_seq
  LEFT JOIN requested AS r ON r.run_ref = d.run_ref
)
SELECT day,
       lane,
       count(DISTINCT event_id) AS n,
       min(wait_seconds) AS min_seconds,
       approx_percentile(wait_seconds, 0.5) AS p50_seconds,
       max(wait_seconds) AS max_seconds
FROM waits
GROUP BY day, lane
ORDER BY day, lane"""

_PAUSE_CONFIDENCE_BY_DECISION = f"""WITH {DEDUPED},
requested AS (
  SELECT DISTINCT run_ref, lane FROM ev WHERE "type" = 'RunRequested'
),
paused AS (
  SELECT run_ref, pause_seq, layer, risk, confidence
  FROM ev
  WHERE "type" = 'RunPaused' AND pause_seq IS NOT NULL
),
decided AS (
  SELECT event_id, run_ref, pause_seq, status, at
  FROM ev
  WHERE "type" = 'DecisionRecorded' AND pause_seq IS NOT NULL
)
SELECT substr(d.at, 1, 10) AS day,
       p.layer AS layer,
       p.risk AS risk,
       CASE WHEN p.confidence IS NULL THEN 'none'
            ELSE format('%.1f', floor(p.confidence * 10) / 10.0) END AS confidence_from,
       d.status AS decision,
       count(DISTINCT d.event_id) AS n
FROM decided AS d
JOIN paused AS p ON p.run_ref = d.run_ref AND p.pause_seq = d.pause_seq
JOIN requested AS r ON r.run_ref = d.run_ref
WHERE r.lane = 'approval'
GROUP BY substr(d.at, 1, 10), p.layer, p.risk,
         CASE WHEN p.confidence IS NULL THEN 'none'
              ELSE format('%.1f', floor(p.confidence * 10) / 10.0) END,
         d.status
ORDER BY day, layer, risk, confidence_from, decision"""

# name -> (description, SQL). Named queries in the lachesis workgroup.
QUERIES: dict[str, tuple[str, str]] = {
    "lachesis-pause-mix-by-layer-per-day": (
        "Paused tool calls (Confirm verdicts) per day by layer and risk class, n only.",
        _PAUSE_MIX,
    ),
    "lachesis-fail-closed-per-day": (
        (
            "Paused calls per day and how many of them were fail-closed (layer unavailable), "
            "as two counts."
        ),
        _FAIL_CLOSED,
    ),
    "lachesis-time-to-decision": (
        (
            "Seconds from RunPaused to the DecisionRecorded that answered it, per day and lane, "
            "with n."
        ),
        _TIME_TO_DECISION,
    ),
    "lachesis-approval-pause-confidence-by-decision": (
        (
            "Approval lane only (try lane excluded): each answered pause by layer, risk, "
            "confidence tenth and the human decision, n only."
        ),
        _PAUSE_CONFIDENCE_BY_DECISION,
    ),
}

"""The schema and the Insight queries of the audit lake (B3), as plain data.

No CDK import, so the offline tests can load this file on its own and
pin it against the publisher (approvals/lambda/publisher.py) without a
synth. infra/stacks/audit.py builds the Glue table and the Athena named
queries from these values.

The lake holds one JSON line per event the audit rule takes from
lachesis-events: the rule target sends only ``$.detail``. Most lines are
the approvals publisher's events (source EVENT_SOURCE), exactly its
whitelisted detail, and the Glue columns are exactly
publisher.DETAIL_FIELDS. Since B7a the rule also takes the activity
ingest's ActivityObserved lines (source ACTIVITY_SOURCE,
approvals/lambda/activity/bus.py), whose fields are not the publisher's:
of them only event_id, type and at are Glue columns, so every query
filters on type and none reads an ActivityObserved line as an approval
event. The partition dt is the day Firehose received the record (UTC),
not a field of the event.

Delivery is at least once (the publisher re-sends, EventBridge and
Firehose retry), so the same event can sit in the lake more than once,
even on two different dt days. Every query reads the events through one
CTE that collapses byte-identical copies with SELECT DISTINCT. That is
not the dedupe key: a copy relayed by an older publisher version can
lack a field a newer one adds (lane, pause_seq, cached), so two copies
of one event can differ. event_id is the only dedupe key. Every count
is count(DISTINCT event_id), and every CTE a query joins on keeps one
row per join key (requested takes max(lane) per run_ref over the rows
that have a lane; decided keeps one row per run_ref and pause_seq).
Days are the event's own ``at`` date, so a copy that arrived after
midnight does not count twice.

What the queries can and cannot say:

- Only paused tool calls reach the bus. Lachesis pauses only on a
  Confirm verdict (Proceed, Guide and Deny never pause a run), so a
  RunPaused row is a Confirm, and its layer says whether Decider,
  a deterministic layer, or nobody (``unavailable``, fail-closed) made
  it. There is no verdict column, and no query invents one. Caveat: the
  runner builds the card, and so the RunPaused fields, from the last
  confirm or deny row of the turn (runner._pending_card). When one turn
  holds a Confirm on one call and a later Deny on another, the row
  describes the denied call, not the paused one.
- A re-issued verdict is not a new Decider verdict. On resume the
  handler re-issues the cached Confirm of the paused call with no model
  call. When that row becomes the pausing row of a new pause, RunPaused
  carries cached = true. The pause-mix, fail-closed and confidence
  queries leave those pauses out (FRESH_PAUSE). A RunPaused relayed
  before the cached field existed has cached null; for those rows a
  heuristic stands in: layer decider with no latency_ms reads as a
  re-issue, since a fresh Decider answer carries the latency the server
  reports. That heuristic holds only if the server always reports
  latency_ms. The time-to-decision query keeps re-issued pauses: a
  person still waited on them.
- The decision on a pause is the status a DecisionRecorded row carries
  (APPROVED or DECLINED, the run status the command left). A pause can
  have more than one: when a resume fails before the agent is invoked
  (domain.Run.resume_not_sent), the run returns to PENDING with the same
  pause_seq and a person decides again. Only the latest decision per
  run_ref and pause_seq (highest seq) is the one the agent received, so
  the queries keep that one and count pauses, not decisions. Its wait
  runs from the pause to that decision, the failed resume included.
- A pause and its decision pair on run_ref plus pause_seq. run_ref is
  the first 32 hex digits of the SHA-256 of the approval_id; the raw id
  is never on the bus or in the lake.
- lane, pause_seq and cached reached the bus in B3. Rows relayed before
  that have none of them, and the queries that need them leave those
  rows out instead of guessing (a missing lane is never read as the
  approval lane).
- The queries return counts with n and dates, and durations in seconds.
  They return no accuracy, rate of agreement or calibration figure.
"""

from __future__ import annotations

EVENT_BUS_NAME = "lachesis-events"
EVENT_SOURCE = "lachesis.approvals"
# B7a: the activity ingest's ActivityObserved events reach the lake too.
# Their lines carry event_id, type and at like every line; their other
# fields are not Glue columns yet, so no query reads them.
ACTIVITY_SOURCE = "lachesis.activity"
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
    ("cached", "boolean"),
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

# A RunPaused that is not a re-issued verdict: cached is false, or,
# for a row relayed before cached existed, the heuristic does not match
# (layer decider with no latency_ms). Written so a null never drops a
# row by accident.
FRESH_PAUSE = (
    "(coalesce(cached, false) = false\n"
    "       AND NOT (cached IS NULL AND coalesce(layer, '') = 'decider'"
    " AND latency_ms IS NULL))"
)

# One lane per run: max over the RunRequested rows that carry one, so a
# copy relayed without lane never fans a run out into a second lane.
_REQUESTED = """requested AS (
  SELECT run_ref, max(lane) AS lane
  FROM ev
  WHERE "type" = 'RunRequested' AND lane IS NOT NULL
  GROUP BY run_ref
)"""

# The latest decision per pause, the one the agent received. An earlier
# one on the same pause_seq was never sent (resume_not_sent).
_DECIDED = """decided AS (
  SELECT run_ref, pause_seq, status, at
  FROM (
    SELECT run_ref, pause_seq, status, at,
           row_number() OVER (PARTITION BY run_ref, pause_seq ORDER BY seq DESC) AS rn
    FROM ev
    WHERE "type" = 'DecisionRecorded' AND pause_seq IS NOT NULL
  ) AS latest
  WHERE rn = 1
)"""

_PAUSE_MIX = f"""WITH {DEDUPED}
SELECT substr(at, 1, 10) AS day,
       layer,
       risk,
       count(DISTINCT event_id) AS n
FROM ev
WHERE "type" = 'RunPaused'
  AND {FRESH_PAUSE}
GROUP BY substr(at, 1, 10), layer, risk
ORDER BY day, layer, risk"""

_FAIL_CLOSED = f"""WITH {DEDUPED}
SELECT substr(at, 1, 10) AS day,
       count(DISTINCT event_id) AS n_paused,
       count(DISTINCT CASE WHEN layer = 'unavailable' THEN event_id END) AS n_fail_closed
FROM ev
WHERE "type" = 'RunPaused'
  AND {FRESH_PAUSE}
GROUP BY substr(at, 1, 10)
ORDER BY day"""

_TIME_TO_DECISION = f"""WITH {DEDUPED},
{_REQUESTED},
paused AS (
  SELECT run_ref, pause_seq, min(event_id) AS event_id, min(at) AS paused_at
  FROM ev
  WHERE "type" = 'RunPaused' AND pause_seq IS NOT NULL
  GROUP BY run_ref, pause_seq
),
{_DECIDED},
waits AS (
  SELECT p.event_id AS event_id,
         substr(d.at, 1, 10) AS day,
         coalesce(r.lane, 'unknown') AS lane,
         date_diff('second', from_iso8601_timestamp(p.paused_at),
                   from_iso8601_timestamp(d.at)) AS wait_seconds
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
{_REQUESTED},
paused AS (
  SELECT event_id, run_ref, pause_seq, layer, risk, confidence
  FROM ev
  WHERE "type" = 'RunPaused' AND pause_seq IS NOT NULL
    AND {FRESH_PAUSE}
),
{_DECIDED}
SELECT substr(d.at, 1, 10) AS day,
       p.layer AS layer,
       p.risk AS risk,
       CASE WHEN p.confidence IS NULL THEN 'none'
            ELSE format('%.1f', floor(p.confidence * 10) / 10.0) END AS confidence_from,
       d.status AS decision,
       count(DISTINCT p.event_id) AS n
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
        (
            "Paused tool calls (Confirm verdicts) per day by layer and risk class, n only. "
            "Re-issued verdicts (no new model call) are left out."
        ),
        _PAUSE_MIX,
    ),
    "lachesis-fail-closed-per-day": (
        (
            "Paused calls per day and how many of them were fail-closed (layer unavailable), "
            "as two counts. Re-issued verdicts are left out."
        ),
        _FAIL_CLOSED,
    ),
    "lachesis-time-to-decision": (
        (
            "Seconds from RunPaused to the latest DecisionRecorded on that pause (the one the "
            "agent received), per day and lane, with n pauses."
        ),
        _TIME_TO_DECISION,
    ),
    "lachesis-approval-pause-confidence-by-decision": (
        (
            "Approval lane only (try lane excluded): each answered pause by layer, risk, "
            "confidence tenth and the latest human decision on it, n pauses only. "
            "Re-issued verdicts are left out."
        ),
        _PAUSE_CONFIDENCE_BY_DECISION,
    ),
}

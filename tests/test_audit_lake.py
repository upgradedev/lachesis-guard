"""The audit lake and its Insight queries (B3), offline.

infra/stacks/audit.py cannot be synthesized here (no CDK), so these
tests pin what can be pinned without it: the schema module
(infra/stacks/audit_schema.py, no CDK import) against the publisher's
whitelisted detail, the SQL of every named query, the constants the
stack reads, and the iac workflow and app wiring.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAMBDA_DIR = ROOT / "approvals" / "lambda"
STACK = ROOT / "infra" / "stacks" / "audit.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


schema = _load("audit_schema", ROOT / "infra" / "stacks" / "audit_schema.py")


@pytest.fixture(scope="module")
def publisher():
    sys.path.insert(0, str(LAMBDA_DIR))
    try:
        return _load("publisher_for_audit", LAMBDA_DIR / "publisher.py")
    finally:
        sys.path.remove(str(LAMBDA_DIR))


# Words a query may use that are not fields: SQL keywords, functions,
# the lake's own names. Everything else in a query is a field or a name
# the query itself defines with AS.
SQL_WORDS = {
    "with", "as", "select", "distinct", "from", "where", "and", "or", "not", "is", "null",
    "group", "by", "order", "join", "left", "on", "case", "when", "then", "else", "end",
    "interval", "day", "current_date", "date_format", "substr", "count", "min", "max",
    "coalesce", "date_diff", "from_iso8601_timestamp", "approx_percentile", "format",
    "floor", "Y", "m", "d", "row_number", "over", "partition", "desc", "true", "false",
}
# Fields an outbox row or a domain event has that the bus must never
# carry, so no query may name them.
NEVER_ON_THE_BUS = {
    "approval_id", "session_id", "decision_text", "decided_by", "decided_by_name",
    "arguments", "prompt", "interrupts", "card", "expires_ts", "ttl", "request_id",
    "verdict", "grounded", "from_status", "error_type", "answer", "cause",
}


def _strip_literals(sql):
    """The SQL without string literals; double-quoted identifiers kept
    as plain words."""
    sql = re.sub(r"'[^']*'", " ", sql)
    return sql.replace('"', " ")


def _words(sql):
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _strip_literals(sql)))


def _defined_names(sql):
    """Names a query defines itself: CTE names and AS aliases."""
    sql = _strip_literals(sql)
    aliases = set(re.findall(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I))
    ctes = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", sql, re.I))
    return aliases | ctes


def test_the_columns_are_exactly_what_the_publisher_emits(publisher):
    assert tuple(name for name, _ in schema.COLUMNS) == publisher.DETAIL_FIELDS
    kinds = dict(schema.COLUMNS)
    assert kinds["seq"] == kinds["pause_seq"] == "bigint"
    assert kinds["confidence"] == kinds["latency_ms"] == "double"
    assert kinds["cached"] == "boolean"
    text = set(publisher.DETAIL_FIELDS) - {"seq", "pause_seq", "confidence", "latency_ms", "cached"}
    assert {kinds[name] for name in text} == {"string"}
    # dt is the Firehose arrival day, a partition key and never a column.
    assert schema.PARTITION_KEY == ("dt", "string")
    assert "dt" not in kinds


@pytest.mark.parametrize("name", sorted(schema.QUERIES))
def test_every_query_references_only_emitted_fields(publisher, name):
    _, sql = schema.QUERIES[name]
    allowed = (
        set(publisher.DETAIL_FIELDS)
        | {"dt", schema.DATABASE, schema.TABLE}
        | {word.lower() for word in SQL_WORDS}
        | SQL_WORDS
        | _defined_names(sql)
    )
    unknown = {word for word in _words(sql) if word not in allowed and word.lower() not in allowed}
    assert unknown == set(), unknown
    assert _words(sql).isdisjoint(NEVER_ON_THE_BUS)
    # A query defines no alias that shadows a field it does not emit.
    assert _defined_names(sql).isdisjoint(NEVER_ON_THE_BUS)


@pytest.mark.parametrize("name", sorted(schema.QUERIES))
def test_every_query_dedupes_and_counts_distinct_event_ids(name):
    _, sql = schema.QUERIES[name]
    assert sql.startswith("WITH " + schema.DEDUPED)
    assert "SELECT DISTINCT" in schema.DEDUPED
    assert "dt >= date_format(current_date - INTERVAL" in schema.DEDUPED
    counts = re.findall(r"count\(([^)]*)\)", sql, re.I)
    assert counts, "a query must count"
    for argument in counts:
        assert argument.strip().upper().startswith("DISTINCT"), argument
        assert "event_id" in argument


@pytest.mark.parametrize("name", sorted(schema.QUERIES))
def test_queries_produce_counts_never_accuracy_or_calibration(name):
    description, sql = schema.QUERIES[name]
    for text in (description.lower(), sql.lower()):
        for word in ("accuracy", "accurate", "calibrat", "precision", "recall", "agreement"):
            assert word not in text
    # No ratio is computed: a query divides only to bucket a confidence.
    assert sql.count("/") == sql.count("/ 10")


def test_the_confidence_query_keeps_only_the_approval_lane():
    _, sql = schema.QUERIES["lachesis-approval-pause-confidence-by-decision"]
    assert "WHERE r.lane = 'approval'" in sql
    # An inner join: a run with no RunRequested lane (relayed before B3)
    # is left out, never counted as the approval lane.
    assert "JOIN requested AS r" in sql and "LEFT JOIN requested" not in sql
    assert "p.pause_seq = d.pause_seq" in sql and "p.run_ref = d.run_ref" in sql
    assert "'try'" not in sql


def test_the_time_query_pairs_each_decision_with_its_own_pause():
    _, sql = schema.QUERIES["lachesis-time-to-decision"]
    assert "p.run_ref = d.run_ref AND p.pause_seq = d.pause_seq" in sql
    assert "'DecisionRecorded'" in sql and "'RunPaused'" in sql


def test_the_fail_closed_query_names_the_unavailable_layer():
    _, sql = schema.QUERIES["lachesis-fail-closed-per-day"]
    assert "layer = 'unavailable'" in sql


def test_event_types_in_queries_exist(publisher):
    for _, sql in schema.QUERIES.values():
        for literal in re.findall(r"\"type\" = '([A-Za-z]+)'", sql):
            assert literal in publisher.EVENT_NAMES


def test_names_match_the_publisher_and_the_task(publisher):
    assert schema.EVENT_BUS_NAME == publisher.BUS_NAME == "lachesis-events"
    assert schema.EVENT_SOURCE == publisher.SOURCE == "lachesis.approvals"
    assert schema.RULE_NAME == "audit-all"
    assert schema.DATABASE == "lachesis_audit" and schema.TABLE == "events"
    assert schema.WORKGROUP == "lachesis"
    assert schema.BYTES_SCANNED_CUTOFF == 1024**3
    assert schema.BUFFER_SECONDS == 60
    assert (schema.EVENTS_EXPIRY_DAYS, schema.RESULTS_EXPIRY_DAYS) == (90, 7)


def test_firehose_prefixes():
    assert schema.DATA_OUTPUT_PREFIX == "events/dt=!{timestamp:yyyy-MM-dd}/"
    assert schema.ERROR_OUTPUT_PREFIX.startswith("errors/")
    # Required by Firehose once the data prefix uses an expression.
    assert "!{firehose:error-output-type}" in schema.ERROR_OUTPUT_PREFIX
    assert schema.DELIMITER == "\\n" and len(schema.DELIMITER) == 2


def test_partition_projection_needs_no_crawler():
    params = schema.table_parameters("audit-bucket")
    assert params["projection.enabled"] == "true"
    assert params["projection.dt.type"] == "date"
    assert params["projection.dt.format"] == "yyyy-MM-dd"
    assert params["storage.location.template"] == "s3://audit-bucket/events/dt=${dt}/"
    assert schema.SERDE == "org.openx.data.jsonserde.JsonSerDe"


def _stack_source():
    return STACK.read_text(encoding="utf-8")


def test_the_stack_uses_no_foundation_and_no_cross_stack_reference():
    source = _stack_source()
    tree = ast.parse(source)
    [cls] = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    init = next(node for node in cls.body if isinstance(node, ast.FunctionDef))
    assert [arg.arg for arg in init.args.args] == ["self", "scope", "construct_id"]
    assert init.args.kwonlyargs == []
    assert "foundation" not in source.replace("Foundation alerts topic", "")
    assert "Fn.import_value" not in source and "CfnOutput" not in source


def test_the_stack_wires_what_the_task_names():
    source = _stack_source()
    for needle in (
        'input_path="$.detail"',
        "dead_letter_config=",
        "sqs.QueueEncryption.SQS_MANAGED",
        '"aws:SourceArn": rule.attr_arn',
        "s3.BlockPublicAccess.BLOCK_ALL",
        "enforce_ssl=True",
        "versioned=True",
        "s3.BucketEncryption.S3_MANAGED",
        'type="AppendDelimiterToRecord"',
        "enforce_work_group_configuration=True",
        "recursive_delete_option=True",
        "table.node.add_dependency(database)",
        "query.node.add_dependency(workgroup, table)",
        # The rule role's trust names this rule, not only the account.
        '"ArnEquals": {"aws:SourceArn": rule_arn}',
        "f\"{schema.EVENT_BUS_NAME}/{schema.RULE_NAME}\"",
        # Both hops alarm: the DLQ (sent and waiting) and Firehose to S3.
        "metric_number_of_messages_sent(",
        "metric_approximate_number_of_messages_visible(",
        'metric_name="DeliveryToS3.DataFreshness"',
        "noncurrent_version_expiration=cdk.Duration.days(NONCURRENT_AUDIT_DAYS)",
    ):
        assert needle in source, needle
    # CfnResource.add_dependency is deprecated; only node.add_dependency.
    assert re.findall(r"(?<!node)\.add_dependency\(", source) == []
    assert source.count("cw_actions.SnsAction(alerts)") == 3
    # The Firehose role writes only under the two prefixes it owns.
    assert 'actions=["s3:PutObject", "s3:AbortMultipartUpload"]' in source
    assert "s3:GetObject" not in source and "s3:DeleteObject" not in source
    assert "grant_write" not in source and "grant_read_write" not in source


def test_the_stack_is_wired_into_the_app_outside_the_import_guard():
    app = (ROOT / "infra" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(app)
    # The import and the instantiation are both top-level statements:
    # neither inside the try nor inside `if HAVE_ALL_STACKS:`, so an
    # ImportError in another stack cannot drop LachesisAudit.
    imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "stacks.audit"
    ]
    assert [alias.name for node in imports for alias in node.names] == ["AuditStack"]
    calls = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "AuditStack"
    ]
    assert len(calls) == 1
    assert ast.literal_eval(calls[0].args[1]) == "LachesisAudit"
    every = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "AuditStack"
    ]
    assert every == calls


def test_the_iac_workflow_deploys_audit_after_the_gate():
    workflow = (ROOT / ".github" / "workflows" / "iac.yml").read_text(encoding="utf-8")
    assert "          - deploy-audit\n" in workflow
    step = "cdk deploy LachesisAudit --exclusively --require-approval never"
    assert step in workflow
    gate = workflow.index("Synth and template gate (before any deploy)")
    assert "if: startsWith(inputs.action, 'deploy-')" in workflow[gate : gate + 200]
    assert gate < workflow.index(step)
    # Destroyed first: its rule sits on the LachesisApprovals bus.
    assert "for stack in LachesisAudit LachesisApprovals" in workflow
    # The redeploy of the judged approvals stack this needs is written
    # where it lasts, beside the step and in the stack docstring.
    before_step = workflow[workflow.index(step) - 400 : workflow.index(step)]
    assert "Run deploy-approvals first" in before_step
    assert "Deploy order: run deploy-approvals first" in _stack_source()


@pytest.mark.parametrize(
    "path",
    [
        "infra/stacks/audit.py",
        "infra/stacks/audit_schema.py",
        "tests/test_audit_lake.py",
        ".github/workflows/iac.yml",
        "infra/app.py",
        "approvals/lambda/publisher.py",
        "approvals/lambda/runner.py",
        "approvals/lambda/approval/domain.py",
        "infra/check_templates.py",
        "tests/test_check_templates.py",
        "tests/test_publisher.py",
    ],
)
def test_no_em_or_en_dashes(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert chr(0x2014) not in text and chr(0x2013) not in text


# --- the queries, run end to end on SQLite ------------------------------
#
# Athena is not available offline, so the SQL runs on SQLite with the
# few Trino functions it uses registered as Python functions. The lake
# rows are built the way the real path builds them: domain commands,
# the real Unit of Work (a recorder stands in for DynamoDB), the outbox
# rows serialized as a stream NEW_IMAGE and mapped by the real publisher.
# Every detail is stored twice, the copy on the next dt day, as an
# at-least-once re-send across midnight would land.

T0 = 1792576800  # 2026-10-21T10:00:00Z
DAY = "2026-10-21"


class _Recorder:
    def __init__(self):
        self.event_rows = []

    def transact_write_items(self, TransactItems):  # noqa: N803
        for entry in TransactItems:
            put = entry.get("Put")
            if put and put["TableName"] == "lachesis-run-events":
                self.event_rows.append(put["Item"])
        return {}


@pytest.fixture()
def lake_rows(monkeypatch):
    from boto3.dynamodb.types import TypeSerializer

    monkeypatch.syspath_prepend(str(LAMBDA_DIR))
    for name in [n for n in sys.modules if n == "approval" or n.startswith("approval.")]:
        monkeypatch.delitem(sys.modules, name)
    common = _load("common", LAMBDA_DIR / "common.py")
    monkeypatch.setitem(sys.modules, "common", common)
    publisher = _load("publisher_for_lake", LAMBDA_DIR / "publisher.py")
    domain = importlib.import_module("approval.domain")
    uow = importlib.import_module("approval.uow")

    clock = {"now": float(T0)}
    recorder = _Recorder()
    work = uow.DynamoUnitOfWork(
        client=recorder,
        table="lachesis-approvals",
        events_table="lachesis-run-events",
        clock=lambda: clock["now"],
    )

    def at(offset, run):
        clock["now"] = float(T0 + offset)
        work.commit(run)

    def card(layer, risk, confidence, latency_ms=150.0, cached=False):
        return {
            "tool": "write_file",
            "risk": risk,
            "confidence": confidence,
            "layer": layer,
            "latency_ms": latency_ms,
            "cached": cached,
        }

    interrupt = [{"interruptId": "i", "name": "lachesis-confirm", "reason": "r"}]
    # An approval-lane run, paused twice: approved after 42 s, then the
    # re-pause declined after 10 s.
    a = domain.Run.request("appr-a", "s" * 40, "approval", T0, 1800)
    at(0, a)
    a.claim("req-a")
    at(1, a)
    a.pause(interrupt, card("decider", "destructive_write", 0.31), T0 + 5, 1800)
    at(5, a)
    a.decide("approve", "sub", "owner", T0 + 47)
    at(47, a)
    a.begin_resume(T0 + 48)
    at(48, a)
    a.pause(interrupt, card("decider", "destructive_write", 0.87), T0 + 60, 1800)
    at(60, a)
    a.decide("decline", "sub", "owner", T0 + 70)
    at(70, a)
    # A /try run, decided after 10 s: in the wait query, never in the
    # approval-lane confidence query.
    b = domain.Run.request("try-b", "t" * 40, "try", T0, 1800)
    at(0, b)
    b.claim("req-b")
    at(1, b)
    b.pause(interrupt, card("decider", "exfiltration", 0.95), T0 + 2, 1800)
    at(2, b)
    b.decide("approve", "try-token", "try-visitor", T0 + 12)
    at(12, b)
    # An approval-lane run paused fail-closed and never decided.
    c = domain.Run.request("appr-c", "u" * 40, "approval", T0, 1800)
    at(0, c)
    c.claim("req-c")
    at(1, c)
    c.pause(interrupt, card("unavailable", "", None, None), T0 + 3, 1800)
    at(3, c)
    # An approval-lane run whose approval was never sent: approved after
    # 5 s, the resume failed before the agent was invoked, the run went
    # back to PENDING with the same pause_seq, and a person declined it
    # at 30 s. Only the decline reached the agent.
    d = domain.Run.request("appr-d", "v" * 40, "approval", T0, 1800)
    at(0, d)
    d.claim("req-d")
    at(1, d)
    d.pause(interrupt, card("decider", "untrusted_to_action", 0.55), T0 + 4, 1800)
    at(4, d)
    d.decide("approve", "sub", "owner", T0 + 9)
    at(9, d)
    d.begin_resume(T0 + 10)
    at(10, d)
    assert d.resume_not_sent(T0 + 11) == "PENDING"
    at(11, d)
    d.decide("decline", "sub", "owner", T0 + 30)
    at(30, d)
    # An approval-lane run re-paused on the handler's cached Confirm (no
    # model call, no latency): the second pause is a real wait for a
    # person but not a second Decider verdict.
    e = domain.Run.request("appr-e", "w" * 40, "approval", T0, 1800)
    at(0, e)
    e.claim("req-e")
    at(1, e)
    e.pause(interrupt, card("decider", "destructive_write", 0.62), T0 + 6, 1800)
    at(6, e)
    e.decide("approve", "sub", "owner", T0 + 16)
    at(16, e)
    e.begin_resume(T0 + 17)
    at(17, e)
    e.pause(interrupt, card("decider", "destructive_write", 0.62, None, True), T0 + 18, 1800)
    at(18, e)
    e.decide("approve", "sub", "owner", T0 + 28)
    at(28, e)

    serializer = TypeSerializer()
    details = []
    for row in recorder.event_rows:
        image = {key: serializer.serialize(value) for key, value in row.items()}
        details.append(publisher.detail_of(publisher.image_of({"dynamodb": {"NewImage": image}})))
    return details


class _Median:
    def __init__(self):
        self.values = []

    def step(self, value, _fraction):
        if value is not None:
            self.values.append(value)

    def finalize(self):
        if not self.values:
            return None
        ordered = sorted(self.values)
        return ordered[(len(ordered) - 1) // 2]


def _run(details, sql):
    import datetime
    import math
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("ATTACH ':memory:' AS lachesis_audit")
    columns = ", ".join(f'"{name}"' for name, _ in schema.COLUMNS)
    db.execute(f"CREATE TABLE lachesis_audit.events ({columns}, dt)")
    for detail in details:
        for dt in (DAY, "2026-10-22"):
            values = [detail.get(name) for name, _ in schema.COLUMNS] + [dt]
            db.execute(
                f"INSERT INTO lachesis_audit.events VALUES ({', '.join('?' * len(values))})",
                values,
            )
    db.create_function(
        "from_iso8601_timestamp",
        1,
        lambda text: datetime.datetime.fromisoformat(text).timestamp(),
    )
    db.create_function("date_diff", 3, lambda unit, start, end: int(end - start))
    db.create_function("floor", 1, math.floor)
    db.create_aggregate("approx_percentile", 2, _Median)
    window = f"date_format(current_date - INTERVAL '{schema.LOOKBACK_DAYS}' DAY, '%Y-%m-%d')"
    assert window in sql
    rows = db.execute(sql.replace(window, "'2000-01-01'")).fetchall()
    db.close()
    return rows


def test_the_lake_rows_carry_lane_pause_seq_and_cached(lake_rows):
    assert {d["lane"] for d in lake_rows if d["type"] == "RunRequested"} == {"approval", "try"}
    assert all("approval_id" not in d for d in lake_rows)
    paused = [d for d in lake_rows if d["type"] == "RunPaused"]
    assert [d["cached"] for d in paused] == [False, False, False, False, False, False, True]
    assert all(isinstance(d["cached"], bool) for d in paused)
    # Run d: two decisions on one pause, the first never sent.
    d_ref = paused[4]["run_ref"]
    decided = [d for d in lake_rows if d["type"] == "DecisionRecorded" and d["run_ref"] == d_ref]
    assert [(x["pause_seq"], x["status"]) for x in decided] == [(1, "APPROVED"), (1, "DECLINED")]


def test_pause_mix_counts_each_fresh_pause_once(lake_rows):
    _, sql = schema.QUERIES["lachesis-pause-mix-by-layer-per-day"]
    # destructive_write: a twice, e once (its re-issued second pause is
    # left out).
    assert _run(lake_rows, sql) == [
        (DAY, "decider", "destructive_write", 3),
        (DAY, "decider", "exfiltration", 1),
        (DAY, "decider", "untrusted_to_action", 1),
        (DAY, "unavailable", "", 1),
    ]


def test_fail_closed_counts(lake_rows):
    _, sql = schema.QUERIES["lachesis-fail-closed-per-day"]
    assert _run(lake_rows, sql) == [(DAY, 6, 1)]


def test_time_to_decision_pairs_each_pause_with_its_latest_decision(lake_rows):
    _, sql = schema.QUERIES["lachesis-time-to-decision"]
    # approval: a 42 and 10 s, d 26 s (pause to the decline that was
    # sent, never the 5 s approval that was not), e 10 and 10 s (the
    # re-issued pause is still a wait); try: 10 s.
    assert _run(lake_rows, sql) == [
        (DAY, "approval", 5, 10, 10, 42),
        (DAY, "try", 1, 10, 10, 10),
    ]


def test_confidence_beside_decision_is_approval_lane_only(lake_rows):
    _, sql = schema.QUERIES["lachesis-approval-pause-confidence-by-decision"]
    # d appears once, with the decision the agent received; e's
    # re-issued pause is left out.
    assert _run(lake_rows, sql) == [
        (DAY, "decider", "destructive_write", "0.3", "APPROVED", 1),
        (DAY, "decider", "destructive_write", "0.6", "APPROVED", 1),
        (DAY, "decider", "destructive_write", "0.8", "DECLINED", 1),
        (DAY, "decider", "untrusted_to_action", "0.5", "DECLINED", 1),
    ]


def test_a_run_relayed_before_lane_existed_is_left_out(lake_rows):
    legacy = [{k: v for k, v in d.items() if k not in {"lane", "pause_seq"}} for d in lake_rows]
    _, sql = schema.QUERIES["lachesis-approval-pause-confidence-by-decision"]
    assert _run(legacy, sql) == []


def test_a_copy_without_lane_never_fans_a_run_out(lake_rows):
    """A RunRequested relayed once by the B2 publisher (no lane) and
    again by B3 (lane) differs, so SELECT DISTINCT keeps both copies;
    the requested CTE must still give one lane per run."""
    old_copies = [
        {k: v for k, v in d.items() if k != "lane"}
        for d in lake_rows
        if d["type"] == "RunRequested"
    ]
    rows = lake_rows + old_copies
    for name in ("lachesis-time-to-decision", "lachesis-approval-pause-confidence-by-decision"):
        _, sql = schema.QUERIES[name]
        assert _run(rows, sql) == _run(lake_rows, sql), name


def test_rows_relayed_before_cached_fall_back_to_the_latency_heuristic(lake_rows):
    """Without the cached field, a decider pause with no latency_ms reads
    as a re-issue, and the counts match the flagged rows."""
    legacy = [{k: v for k, v in d.items() if k != "cached"} for d in lake_rows]
    for name in (
        "lachesis-pause-mix-by-layer-per-day",
        "lachesis-fail-closed-per-day",
        "lachesis-approval-pause-confidence-by-decision",
    ):
        _, sql = schema.QUERIES[name]
        assert _run(legacy, sql) == _run(lake_rows, sql), name


def test_the_cached_flag_wins_over_the_heuristic(lake_rows):
    """A fresh decider pause whose server sent no latency stays counted
    when the flag says it is fresh."""
    no_latency = [
        dict(d, latency_ms=None) if d["type"] == "RunPaused" and not d["cached"] else d
        for d in lake_rows
    ]
    _, sql = schema.QUERIES["lachesis-fail-closed-per-day"]
    assert _run(no_latency, sql) == [(DAY, 6, 1)]


def test_the_queries_name_the_fresh_pause_filter():
    for name in (
        "lachesis-pause-mix-by-layer-per-day",
        "lachesis-fail-closed-per-day",
        "lachesis-approval-pause-confidence-by-decision",
    ):
        assert schema.FRESH_PAUSE in schema.QUERIES[name][1], name
    # A person waited on a re-issued pause too.
    assert schema.FRESH_PAUSE not in schema.QUERIES["lachesis-time-to-decision"][1]
    for name in ("lachesis-time-to-decision", "lachesis-approval-pause-confidence-by-decision"):
        sql = schema.QUERIES[name][1]
        assert "PARTITION BY run_ref, pause_seq ORDER BY seq DESC" in sql
        assert "WHERE rn = 1" in sql
        assert "\"type\" = 'RunRequested' AND lane IS NOT NULL" in sql

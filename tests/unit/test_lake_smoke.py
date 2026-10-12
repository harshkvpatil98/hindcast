"""`cascade aws smoke-test`: synthetic events round the event lake (ADR-0054).

The command proves the deployed lake's wiring -- S3, KMS, Glue, Athena and the
two roles -- with three events that say they are synthetic. Driven here through
fake sessions, so no test needs an account. What is asserted is what would make
the smoke test worthless if it broke: the file's columns are the Glue table's,
the row is the row Postgres stores, the writer's refused delete is *required*,
and a query that succeeds with the wrong rows is a failure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cascade.cli import app
from cascade.config import Settings
from cascade.trace import lake
from cascade.trace.lake import (
    LAKE_COLUMNS,
    SMOKE_CONFIG_ID,
    SMOKE_RUN_ID,
    LakeTarget,
    SmokeReport,
    SmokeStep,
    expected_result_rows,
    lake_row,
    object_key,
    parquet_bytes,
    run_smoke_test,
    smoke_query,
    synthetic_events,
)
from cascade.version import EXIT_OK, EXIT_PRECONDITION

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[2]
REGION = "us-west-2"
ACCOUNT = "123456789012"
IDENTITY = {
    "Account": ACCOUNT,
    "Arn": f"arn:aws:iam::{ACCOUNT}:user/operator",
    "ResponseMetadata": {"RequestId": "req-identity"},
}


def client_error(code: str, operation: str, request_id: str = "req-error") -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": code, "Message": f"{code} on {operation}"},
            "ResponseMetadata": {"RequestId": request_id},
        },
        operation,
    )


class FakeClient:
    """Answers each operation from a table; a list is consumed one call at a time."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, operation: str) -> Any:
        if operation not in self.answers:
            raise AttributeError(operation)

        def call(**kwargs: Any) -> Any:
            self.calls.append((operation, kwargs))
            answer = self.answers[operation]
            if isinstance(answer, list):
                answer = answer.pop(0) if len(answer) > 1 else answer[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return call

    def operations(self) -> list[str]:
        return [operation for operation, _ in self.calls]


class FakeSession:
    def __init__(self, label: str, **clients: FakeClient) -> None:
        self.label = label
        self.clients = clients
        self.regions: dict[str, str | None] = {}

    def client(self, service: str, *, region_name: str | None = None, **_: Any) -> FakeClient:
        self.regions[service] = region_name
        return self.clients[service]


def credentials(role: str) -> dict[str, Any]:
    return {
        "Credentials": {
            "AccessKeyId": f"AKIA-{role}",
            "SecretAccessKey": "secret",
            "SessionToken": "token",
        },
        "ResponseMetadata": {"RequestId": f"req-assume-{role}"},
    }


def athena_results(rows: tuple[tuple[str | None, ...], ...]) -> dict[str, Any]:
    def cells(values: tuple[str | None, ...]) -> dict[str, Any]:
        return {"Data": [{} if value is None else {"VarCharValue": value} for value in values]}

    header = cells(("run_id", "step", "seq", "actor_id", "action", "cache_hit", "tokens_in", "c"))
    return {
        "ResultSet": {"Rows": [header, *[cells(row) for row in rows]]},
        "ResponseMetadata": {"RequestId": "req-results"},
    }


def execution(state: str, reason: str = "") -> dict[str, Any]:
    status: dict[str, Any] = {"State": state}
    if reason:
        status["StateChangeReason"] = reason
    return {"QueryExecution": {"Status": status, "Statistics": {"DataScannedInBytes": 2048}}}


class World:
    """One fake account: an operator session and the two sessions its roles assume into."""

    def __init__(self, **overrides: Any) -> None:
        rows = overrides.pop("rows", expected_result_rows(synthetic_events()))
        self.writer_s3 = FakeClient(
            {
                "put_object": {
                    "VersionId": "v-1",
                    "ServerSideEncryption": "aws:kms",
                    "ResponseMetadata": {"RequestId": "req-put"},
                },
                "delete_object": overrides.pop(
                    "delete", client_error("AccessDenied", "DeleteObject", "req-denied")
                ),
            }
        )
        self.athena = FakeClient(
            {
                "start_query_execution": {
                    "QueryExecutionId": "query-1",
                    "ResponseMetadata": {"RequestId": "req-start"},
                },
                "get_query_execution": overrides.pop(
                    "executions", [execution("RUNNING"), execution("SUCCEEDED")]
                ),
                "get_query_results": athena_results(rows),
            }
        )
        self.sts = FakeClient(
            {
                "get_caller_identity": IDENTITY,
                "assume_role": overrides.pop(
                    "assume", [credentials("writer"), credentials("analyst")]
                ),
            }
        )
        self.glue = FakeClient(
            {
                "get_table": {
                    "Table": {
                        "StorageDescriptor": {
                            "Location": f"s3://cascade-events-{ACCOUNT}-{REGION}/events/",
                            "SerdeInfo": {"SerializationLibrary": "parquet"},
                        }
                    }
                },
                "create_partition": overrides.pop(
                    "partition", {"ResponseMetadata": {"RequestId": "req-partition"}}
                ),
            }
        )
        assert not overrides, overrides
        self.operator = FakeSession("operator", sts=self.sts, glue=self.glue)
        self.writer = FakeSession("writer", s3=self.writer_s3)
        self.analyst = FakeSession("analyst", athena=self.athena)
        self.made: list[str] = []
        self.slept: list[float] = []

    def factory(self, access_key: str, secret_key: str, token: str) -> FakeSession:
        self.made.append(access_key)
        return self.writer if access_key.endswith("writer") else self.analyst

    def run(self, settings: Settings) -> SmokeReport:
        return run_smoke_test(
            settings, session=self.operator, session_factory=self.factory, sleep=self.slept.append
        )


def by_name(report: SmokeReport) -> dict[str, SmokeStep]:
    return {step.name: step for step in report.steps}


# --- What is written ------------------------------------------------------------------


def test_the_files_columns_are_the_glue_tables_columns() -> None:
    """The Parquet file is read through the Terraform's table: the two lists must be one list."""
    terraform = (REPO_ROOT / "infra/terraform/modules/eventlake/main.tf").read_text("utf-8")
    block = terraform[terraform.index('dynamic "columns"') :]
    block = block[: block.index("content {")]
    declared = tuple(re.findall(r'\["(\w+)", "(\w+)"\]', block))
    assert declared == LAKE_COLUMNS
    assert len(declared) == 13


def test_the_lake_row_is_the_row_postgres_stores() -> None:
    """One projection of the event log: a value in Athena equals the value in the database."""
    from cascade.trace.store import _event_row

    for event in synthetic_events():
        assert lake_row(event) == _event_row(event)
        assert len(lake_row(event)) == len(LAKE_COLUMNS)


def test_the_events_say_they_are_synthetic_wherever_a_reader_would_look() -> None:
    events = synthetic_events()
    assert len(events) == 3
    for event in events:
        assert event.run_id == SMOKE_RUN_ID and "smoke" in event.run_id
        assert event.actor_id.startswith("synthetic-")
        assert event.action["synthetic"] is True
        # No model was called, so no token was spent.
        assert event.tokens_in == 0 and event.tokens_out == 0
    # A partition no ablation cell can be: cells are C01..C12 and S01.
    assert not re.fullmatch(r"[CS]\d\d", SMOKE_CONFIG_ID)
    assert synthetic_events() == events


def test_the_parquet_file_has_the_tables_types_and_keeps_a_null() -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    import io

    table = pq.read_table(io.BytesIO(parquet_bytes(synthetic_events())))
    arrow = {
        "string": pa.string(),
        "smallint": pa.int16(),
        "int": pa.int32(),
        "binary": pa.binary(),
        "boolean": pa.bool_(),
    }
    assert [(field.name, field.type) for field in table.schema] == [
        (name, arrow[kind]) for name, kind in LAKE_COLUMNS
    ]
    rows = [tuple(row[name] for name, _ in LAKE_COLUMNS) for row in table.to_pylist()]
    assert rows == [lake_row(event) for event in synthetic_events()]
    # The first event has no latency and no coercion: both must come back null,
    # typed, not inferred as something else from an all-null column.
    assert rows[0][11] is None and rows[0][12] is None


def test_the_object_is_named_by_what_it_holds_under_the_smoke_partition() -> None:
    key = object_key(synthetic_events())
    assert key == object_key(tuple(reversed(synthetic_events())))
    assert re.fullmatch(rf"events/config_id={SMOKE_CONFIG_ID}/smoke-[0-9a-f]{{16}}\.parquet", key)


def test_expected_rows_are_rendered_as_athena_renders_them() -> None:
    rows = expected_result_rows(synthetic_events())
    assert [row[1:3] for row in rows] == [("0", "0"), ("1", "0"), ("1", "1")]
    assert [row[5] for row in rows] == ["false", "true", "false"]
    assert rows[0][-1] is None and rows[2][-1] == "synthetic: smoke test"
    assert json.loads(rows[1][4] or "")["synthetic"] is True


def test_a_name_that_is_not_an_identifier_never_reaches_the_query() -> None:
    with pytest.raises(ValueError, match="lower-case"):
        LakeTarget(partition="aws", account=ACCOUNT, region=REGION, name='x"; DROP TABLE events')
    target = LakeTarget(partition="aws", account=ACCOUNT, region=REGION, name="cascade-dev")
    assert target.database == "cascade_dev"
    assert target.bucket == f"cascade-dev-events-{ACCOUNT}-{REGION}"
    assert '"cascade_dev"."events"' in smoke_query(target)
    assert f"config_id = '{SMOKE_CONFIG_ID}'" in smoke_query(target)


# --- The round trip ----------------------------------------------------------------------


def test_the_round_trip_completes_and_every_step_keeps_its_request_id(settings: Settings) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("botocore")
    world = World()
    report = world.run(settings)

    assert report.ok, [step for step in report.steps if not step.ok]
    assert [step.name for step in report.steps] == [
        "region",
        "parquet",
        "identity",
        "assume writer",
        "write events",
        "writer cannot delete",
        "register partition",
        "assume analyst",
        "query",
        "round trip",
    ]
    steps = by_name(report)
    assert steps["identity"].request_id == "req-identity"
    assert steps["assume writer"].request_id == "req-assume-writer"
    assert steps["write events"].request_id == "req-put"
    assert steps["writer cannot delete"].request_id == "req-denied"
    assert steps["register partition"].request_id == "req-partition"
    assert steps["query"].request_id == "req-start"
    assert steps["round trip"].request_id == "req-results"
    assert report.query_execution_id == "query-1" and report.data_scanned_bytes == 2048
    assert report.rows == expected_result_rows(synthetic_events())
    assert report.object_key == object_key(synthetic_events())


def test_each_identity_does_its_own_part_in_the_configured_region(settings: Settings) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("botocore")
    world = World()
    world.run(settings)

    # Every client, on every session, is built for the configured region.
    assert world.operator.regions == {"sts": REGION, "glue": REGION}
    assert world.writer.regions == {"s3": REGION}
    assert world.analyst.regions == {"athena": REGION}
    # The writer writes; the operator changes the catalog; the analyst queries.
    assert world.writer_s3.operations() == ["put_object", "delete_object"]
    assert world.glue.operations() == ["get_table", "create_partition"]
    assert world.athena.operations()[0] == "start_query_execution"
    assert world.made == ["AKIA-writer", "AKIA-analyst"]

    put = world.writer_s3.calls[0][1]
    assert put["Bucket"] == f"cascade-events-{ACCOUNT}-{REGION}"
    assert put["Key"].startswith(f"events/config_id={SMOKE_CONFIG_ID}/")
    partition = world.glue.calls[1][1]
    assert partition["PartitionInput"]["Values"] == [SMOKE_CONFIG_ID]
    assert partition["PartitionInput"]["StorageDescriptor"]["Location"].endswith(
        f"/events/config_id={SMOKE_CONFIG_ID}/"
    )
    start = world.athena.calls[0][1]
    assert start["WorkGroup"] == "cascade"
    assert start["QueryExecutionContext"] == {"Database": "cascade"}
    assert world.sts.calls[1][1]["RoleArn"] == f"arn:aws:iam::{ACCOUNT}:role/cascade-lake-writer"
    assert world.sts.calls[2][1]["RoleArn"] == f"arn:aws:iam::{ACCOUNT}:role/cascade-lake-analyst"
    # One poll found the query running, so the wait was taken once -- and not from a real clock.
    assert world.slept == [1.0]


def test_a_writer_that_can_delete_fails_the_test_and_stops_it(settings: Settings) -> None:
    """The refused delete is the lake's one append-only control without the lock: it is required."""
    pytest.importorskip("pyarrow")
    pytest.importorskip("botocore")
    world = World(delete={"ResponseMetadata": {"RequestId": "req-deleted"}})
    report = world.run(settings)

    assert not report.ok
    step = by_name(report)["writer cannot delete"]
    assert not step.ok and "invariant 6" in step.detail and step.request_id == "req-deleted"
    assert world.glue.calls == [] and world.athena.calls == []


def test_a_refusal_that_is_not_access_denied_is_not_the_refusal_asked_for(
    settings: Settings,
) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("botocore")
    world = World(delete=client_error("SlowDown", "DeleteObject"))
    report = world.run(settings)
    assert not report.ok and "SlowDown" in by_name(report)["writer cannot delete"].detail


def test_rows_that_differ_from_what_was_written_fail_a_query_that_succeeded(
    settings: Settings,
) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("botocore")
    wrong = expected_result_rows(synthetic_events())[:2]
    report = World(rows=wrong).run(settings)

    steps = by_name(report)
    assert steps["query"].ok
    assert not steps["round trip"].ok and "differ" in steps["round trip"].detail
    assert not report.ok


def test_a_failed_query_is_reported_with_athenas_reason(settings: Settings) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("botocore")
    world = World(executions=[execution("FAILED", "Insufficient permissions on the key")])
    report = world.run(settings)

    assert not report.ok and report.query_execution_id == "query-1"
    step = by_name(report)["query"]
    assert "FAILED" in step.detail and "Insufficient permissions" in step.detail
    assert "round trip" not in by_name(report)


def test_a_role_that_cannot_be_assumed_stops_the_test_with_the_request_id(
    settings: Settings,
) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("botocore")
    world = World(assume=[client_error("AccessDenied", "AssumeRole", "req-no-assume")])
    report = world.run(settings)

    assert not report.ok
    step = by_name(report)["assume writer"]
    assert "AccessDenied" in step.detail and step.request_id == "req-no-assume"
    assert world.writer_s3.calls == []


def test_a_partition_that_is_already_registered_is_not_a_failure(settings: Settings) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("botocore")
    world = World(partition=client_error("AlreadyExistsException", "CreatePartition"))
    report = world.run(settings)
    assert report.ok
    assert "already registered" in by_name(report)["register partition"].detail


def test_no_region_is_the_only_finding_and_nothing_is_asked(settings: Settings) -> None:
    world = World()
    unrouted = settings.model_copy(
        update={"observability": settings.observability.model_copy(update={"aws_region": None})}
    )
    report = world.run(unrouted)
    assert not report.ok
    assert [step.name for step in report.steps] == ["region"]
    assert world.sts.calls == []


def test_without_pyarrow_the_precondition_is_named_and_nothing_is_sent(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("botocore")

    def absent(events: Any) -> bytes:
        raise ImportError("No module named 'pyarrow'")

    monkeypatch.setattr(lake, "parquet_bytes", absent)
    world = World()
    report = world.run(settings)
    assert not report.ok
    assert "uv sync --extra analytics" in by_name(report)["parquet"].detail
    assert world.sts.calls == []


def test_a_run_that_stops_early_is_not_a_pass() -> None:
    truncated = SmokeReport(
        region=REGION, steps=(SmokeStep(name="region", ok=True, detail=REGION),)
    )
    assert not truncated.ok


def test_the_smoke_test_builds_no_model_serving_client() -> None:
    """Invariant 5: S3, Glue, Athena and STS only. Nothing here can invoke a model."""
    import inspect

    source = inspect.getsource(lake)
    for service in ("bedrock", "bedrock-runtime", "bedrock-agent-runtime", "sagemaker-runtime"):
        assert f'"{service}"' not in source
    for operation in ("invoke_model", "apply_guardrail", "messages.create"):
        assert operation not in source


# --- The command ---------------------------------------------------------------------------


def passing_report() -> SmokeReport:
    return SmokeReport(
        region=REGION,
        steps=(
            SmokeStep(name="region", ok=True, detail=REGION),
            SmokeStep(name="write events", ok=True, detail="s3://b/k", request_id="req-put"),
            SmokeStep(name="round trip", ok=True, detail="3 rows returned", request_id="req-res"),
        ),
        object_key="events/config_id=SMOKE/smoke-0.parquet",
        query_execution_id="query-1",
        data_scanned_bytes=2048,
        rows=expected_result_rows(synthetic_events()),
    )


def test_the_command_exits_zero_prints_request_ids_and_writes_the_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(lake, "run_smoke_test", lambda settings, name: passing_report())
    record = tmp_path / "logs" / "smoke.json"
    result = runner.invoke(app, ["aws", "smoke-test", "--record", str(record)])

    assert result.exit_code == EXIT_OK, result.output
    assert "req-put" in result.output and "synthetic" in result.output
    assert "not the event export" in result.output
    written = json.loads(record.read_text("utf-8"))
    assert written["ok"] is True and written["query_execution_id"] == "query-1"
    assert written["steps"][1]["request_id"] == "req-put"


def test_the_command_exits_three_when_the_round_trip_does_not_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = SmokeReport(
        region=REGION,
        steps=(
            SmokeStep(name="region", ok=True, detail=REGION),
            SmokeStep(name="identity", ok=False, detail="NoCredentialsError: none"),
        ),
    )
    monkeypatch.setattr(lake, "run_smoke_test", lambda settings, name: failing)
    result = runner.invoke(app, ["aws", "smoke-test"])
    assert result.exit_code == EXIT_PRECONDITION
    assert "NoCredentialsError" in result.output

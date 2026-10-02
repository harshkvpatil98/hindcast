"""The event lake's smoke test: synthetic events to S3, through Glue, back out of Athena.

**This is not the event export.** Nothing in this repository moves the real
event log from Postgres to the lake yet (ADR-0054 says so, under "Not
verified"). What this module proves is narrower and worth having on its own:
that the deployed pieces fit together -- the events bucket, its CMK, the Glue
table, the Athena workgroup and the two roles -- by sending three events that
are *labelled synthetic in every field a reader would look at* all the way
round and comparing what comes back with what went in.

The events are real :class:`~cascade.trace.events.DecisionEvent` objects and
the row is the row Postgres stores (``store._event_row``; a test holds the two
equal), so the file's columns are the Glue table's columns because both are
migration 009's. They are written under ``config_id=SMOKE``, a partition no
ablation cell can collide with, by a run id that says what it is.

Who does what, which is the part worth demonstrating:

* **the writer role** puts the object, and is then asked to delete it. The
  delete must be refused. With Object Lock off in the portfolio profile that
  explicit Deny is the lake's one remaining append-only control (invariant 6),
  so the smoke test asserts it live rather than trusting the policy text;
* **the operator** -- whoever runs the command -- registers the partition.
  Neither lake role may change the catalog, and a real export would need an
  identity that can; that is one of the things still to build;
* **the analyst role** runs one query through the workgroup, which is the only
  way that role can run one.

Routing is explicit and never ambient (ADR-0028): every client is built for
``observability.aws_region`` and configured endpoint URLs are ignored.
Credentials are ambient, as everywhere else. No client here is for a
model-serving service, so invariant 5 has nothing to say about this module;
a test asserts that stays true.

Cost: one object of a few kilobytes, one catalog call, one Athena query that
scans that object. Athena bills a query by bytes scanned with a 10 MB minimum.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from cascade.config import Settings
from cascade.trace.events import DecisionEvent

__all__ = [
    "LAKE_COLUMNS",
    "SMOKE_CONFIG_ID",
    "SMOKE_RUN_ID",
    "LakeTarget",
    "SmokeReport",
    "SmokeStep",
    "expected_result_rows",
    "lake_row",
    "object_key",
    "parquet_bytes",
    "run_smoke_test",
    "smoke_query",
    "synthetic_events",
]

# The Glue table's columns, in order (infra/terraform/modules/eventlake). A
# test parses the Terraform and holds this equal to it, so the file this
# module writes cannot drift from the table Athena reads it through.
LAKE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "string"),
    ("step", "smallint"),
    ("seq", "smallint"),
    ("actor_id", "string"),
    ("obs_hash", "binary"),
    ("action", "string"),
    ("caused_by", "string"),
    ("factor_delta", "string"),
    ("cache_hit", "boolean"),
    ("tokens_in", "int"),
    ("tokens_out", "int"),
    ("latency_ms", "int"),
    ("coercion", "string"),
)

# A partition no ablation cell uses (cells are C01..C12 and S01), and a run id
# no real run can have: run ids are derived digests, never words.
SMOKE_CONFIG_ID = "SMOKE"
SMOKE_RUN_ID = "smoke-test-synthetic-run"

# What the query asks for, in the order it asks: enough to show each Parquet
# type survived the trip (string, smallint, boolean, int and a null).
_QUERY_COLUMNS = (
    "run_id",
    "step",
    "seq",
    "actor_id",
    "action",
    "cache_hit",
    "tokens_in",
    "coercion",
)

_ROLE_SESSION_SECONDS = 900
_QUERY_POLL_SECONDS = 1.0
_QUERY_POLLS = 60


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LakeTarget(_Frozen):
    """Where the lake is, by modules/eventlake's naming.

    Preserves the rule that names are derived once: every name here is the
    expression the Terraform module uses, from the same three inputs, so the
    command needs no output file from a `terraform apply` to find the lake.
    """

    partition: str
    account: str
    region: str
    name: str

    @field_validator("name")
    @classmethod
    def _name_is_an_identifier(cls, value: str) -> str:
        # The name becomes a database identifier inside the one SQL statement
        # this module sends, and identifiers cannot be bound as parameters.
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", value):
            raise ValueError(
                "name must be lower-case letters, digits and hyphens, starting with a letter"
            )
        return value

    @property
    def bucket(self) -> str:
        return f"{self.name}-events-{self.account}-{self.region}"

    @property
    def database(self) -> str:
        return self.name.replace("-", "_")

    @property
    def table(self) -> str:
        return "events"

    @property
    def workgroup(self) -> str:
        return self.name

    @property
    def writer_role_arn(self) -> str:
        return f"arn:{self.partition}:iam::{self.account}:role/{self.name}-lake-writer"

    @property
    def analyst_role_arn(self) -> str:
        return f"arn:{self.partition}:iam::{self.account}:role/{self.name}-lake-analyst"

    @property
    def partition_location(self) -> str:
        return f"s3://{self.bucket}/events/config_id={SMOKE_CONFIG_ID}/"


class SmokeStep(_Frozen):
    """One step of the smoke test, with the request id AWS gave it."""

    name: str
    ok: bool
    detail: str
    request_id: str | None = None


class SmokeReport(_Frozen):
    """What the smoke test did, step by step, and the rows Athena returned."""

    region: str | None
    steps: tuple[SmokeStep, ...]
    object_key: str | None = None
    query_execution_id: str | None = None
    data_scanned_bytes: int | None = None
    rows: tuple[tuple[str | None, ...], ...] = ()

    @property
    def ok(self) -> bool:
        """True only for a completed round trip: a run that stopped early is not a pass."""
        return all(step.ok for step in self.steps) and any(
            step.name == "round trip" for step in self.steps
        )


def synthetic_events() -> tuple[DecisionEvent, ...]:
    """Three events that say they are synthetic wherever a reader would look.

    Preserves the measured-values rule at the lake: nothing here can be
    mistaken for a decision a model took. The actor ids, the action payloads
    and the run id all name the smoke test, the token counts are zero because
    no model was called, and nothing is random, so two runs write the same
    object.
    """

    def observation(step: int) -> bytes:
        return hashlib.blake2b(
            f"cascade smoke test, synthetic observation {step}".encode(), digest_size=16
        ).digest()

    first = DecisionEvent(
        run_id=SMOKE_RUN_ID,
        step=0,
        seq=0,
        actor_id="synthetic-actor-a",
        obs_hash=observation(0),
        action={"kind": "WAIT", "synthetic": True, "note": "smoke test, not a decision"},
        cache_hit=False,
        tokens_in=0,
        tokens_out=0,
        latency_ms=None,
    )
    second = DecisionEvent(
        run_id=SMOKE_RUN_ID,
        step=1,
        seq=0,
        actor_id="synthetic-actor-b",
        obs_hash=observation(1),
        action={"kind": "INFLUENCE", "synthetic": True, "note": "smoke test, not a decision"},
        caused_by=(first.ref,),
        factor_delta={"synthetic_factor": 0.04},
        cache_hit=True,
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
    )
    third = DecisionEvent(
        run_id=SMOKE_RUN_ID,
        step=1,
        seq=1,
        actor_id="synthetic-actor-a",
        obs_hash=observation(1),
        action={"kind": "WAIT", "synthetic": True, "note": "smoke test, not a decision"},
        caused_by=(first.ref, second.ref),
        cache_hit=False,
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
        coercion="synthetic: smoke test",
    )
    return (first, second, third)


def lake_row(event: DecisionEvent) -> tuple[Any, ...]:
    """One event as the lake's row: the row Postgres stores, column for column.

    Preserves one projection for the event log. The three JSON columns are the
    same sorted-key text ``store._event_row`` hands to Postgres as jsonb, so a
    value read out of Athena equals the value read out of the database.
    """
    return (
        event.run_id,
        event.step,
        event.seq,
        event.actor_id,
        event.obs_hash,
        json.dumps(event.action, sort_keys=True),
        json.dumps([ref.as_json() for ref in event.caused_by], sort_keys=True),
        json.dumps(event.factor_delta, sort_keys=True),
        event.cache_hit,
        event.tokens_in,
        event.tokens_out,
        event.latency_ms,
        event.coercion,
    )


def _ordered(events: Sequence[DecisionEvent]) -> list[DecisionEvent]:
    return sorted(events, key=lambda event: (event.step, event.seq))


def parquet_bytes(events: Sequence[DecisionEvent]) -> bytes:
    """The events as one Parquet file whose schema is the Glue table's.

    Preserves schema agreement by construction: every column's Arrow type is
    looked up from :data:`LAKE_COLUMNS`, never chosen per value, so a null
    column cannot be inferred as the wrong type. Raises ``ImportError`` when
    the ``analytics`` extra is absent; the caller reports that as a
    precondition, not as a failure of the lake.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    arrow = {
        "string": pa.string(),
        "smallint": pa.int16(),
        "int": pa.int32(),
        "binary": pa.binary(),
        "boolean": pa.bool_(),
    }
    rows = [lake_row(event) for event in _ordered(events)]
    schema = pa.schema([pa.field(name, arrow[kind]) for name, kind in LAKE_COLUMNS])
    arrays = [
        pa.array([row[index] for row in rows], type=schema.field(index).type)
        for index in range(len(LAKE_COLUMNS))
    ]
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_arrays(arrays, schema=schema), sink, compression="snappy")
    return sink.getvalue()


def object_key(events: Sequence[DecisionEvent]) -> str:
    """Where the file goes: under the SMOKE partition, named by what it holds.

    Preserves idempotence: the name is a digest of the rows, not of the
    encoded file and not of a clock, so running the test twice replaces one
    object rather than adding a second copy of the same three events.
    """
    rows = [
        [value.hex() if isinstance(value, bytes) else value for value in lake_row(event)]
        for event in _ordered(events)
    ]
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16]
    return f"events/config_id={SMOKE_CONFIG_ID}/smoke-{digest}.parquet"


def smoke_query(target: LakeTarget) -> str:
    """The one query the analyst runs: this partition only, a handful of columns."""
    columns = ", ".join(_QUERY_COLUMNS)
    # Nothing here is caller text: the columns and the two values are this
    # module's constants, and the database name is validated by LakeTarget.
    return (
        f'SELECT {columns} FROM "{target.database}"."{target.table}" '  # noqa: S608
        f"WHERE config_id = '{SMOKE_CONFIG_ID}' AND run_id = '{SMOKE_RUN_ID}' "
        "ORDER BY step, seq"
    )


def expected_result_rows(events: Sequence[DecisionEvent]) -> tuple[tuple[str | None, ...], ...]:
    """What Athena must return for :func:`smoke_query`, as Athena renders it.

    Preserves a real comparison: the test passes on these exact values coming
    back, not on "some rows came back". Athena renders every value as text, a
    boolean in lower case and a null as an absent value.
    """
    names = [name for name, _ in LAKE_COLUMNS]
    picks = [names.index(column) for column in _QUERY_COLUMNS]

    def render(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return tuple(
        tuple(render(lake_row(event)[index]) for index in picks) for event in _ordered(events)
    )


def _request_id(response: Any) -> str | None:
    metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
    value = metadata.get("RequestId")
    return str(value) if value else None


def run_smoke_test(
    settings: Settings,
    *,
    name: str = "cascade",
    session: Any | None = None,
    session_factory: Callable[[str, str, str], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SmokeReport:
    """Send the synthetic events round the lake and say what came back.

    Preserves three things. Routing is explicit (ADR-0028): every client is
    built for ``observability.aws_region``. Absence is never success: a step
    that could not run is a failed step, a run that stops early is not ``ok``,
    and rows that differ from what was written fail the round trip even though
    a query succeeded. And the writer's Deny is asserted, not assumed: a
    delete that is *allowed* fails the test.

    Only botocore's own exceptions are caught, per step, so an operator sees
    which step AWS refused and the request id it refused it under; anything
    else is a bug and propagates.
    """
    region = settings.observability.aws_region
    if not region:
        return SmokeReport(
            region=None,
            steps=(
                SmokeStep(
                    name="region",
                    ok=False,
                    detail="observability.aws_region is not set; refusing to let the ambient "
                    "environment choose the region (ADR-0028)",
                ),
            ),
        )
    steps: list[SmokeStep] = [SmokeStep(name="region", ok=True, detail=region)]

    def report(**fields: Any) -> SmokeReport:
        return SmokeReport(region=region, steps=tuple(steps), **fields)

    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        steps.append(
            SmokeStep(
                name="boto3",
                ok=False,
                detail="boto3 is not installed: `uv sync --extra aws` (ADR-0028)",
            )
        )
        return report()

    events = synthetic_events()
    try:
        body = parquet_bytes(events)
    except ImportError:
        steps.append(
            SmokeStep(
                name="parquet",
                ok=False,
                detail="pyarrow is not installed: `uv sync --extra analytics`",
            )
        )
        return report()
    key = object_key(events)
    steps.append(
        SmokeStep(
            name="parquet",
            ok=True,
            detail=f"{len(events)} synthetic events, {len(body)} bytes, {len(LAKE_COLUMNS)} columns",
        )
    )

    if session is None:
        session = boto3.session.Session(profile_name=settings.providers.bedrock.profile or None)
    if session_factory is None:

        def session_factory(access_key: str, secret_key: str, token: str) -> Any:
            return boto3.session.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                aws_session_token=token,
            )

    config = Config(
        region_name=region,
        ignore_configured_endpoint_urls=True,
        retries={"max_attempts": 3, "mode": "standard"},
        connect_timeout=10,
        read_timeout=30,
    )

    def client(owner: Any, service: str) -> Any:
        return owner.client(service, region_name=region, config=config)

    def failed(step: str, exc: Exception) -> SmokeReport:
        response = getattr(exc, "response", None)
        steps.append(
            SmokeStep(
                name=step,
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
                request_id=_request_id(response),
            )
        )
        return report(object_key=key)

    def assume(role_arn: str, step: str) -> Any:
        response = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="cascade-smoke-test",
            DurationSeconds=_ROLE_SESSION_SECONDS,
        )
        credentials = response["Credentials"]
        steps.append(
            SmokeStep(name=step, ok=True, detail=role_arn, request_id=_request_id(response))
        )
        return session_factory(
            credentials["AccessKeyId"], credentials["SecretAccessKey"], credentials["SessionToken"]
        )

    # --- Who is running this, and so where the lake is ----------------------------
    sts = client(session, "sts")
    try:
        identity = sts.get_caller_identity()
    except (BotoCoreError, ClientError) as exc:
        return failed("identity", exc)
    target = LakeTarget(
        partition=str(identity["Arn"]).split(":")[1],
        account=str(identity["Account"]),
        region=region,
        name=name,
    )
    steps.append(
        SmokeStep(
            name="identity",
            ok=True,
            detail=f"account {target.account}, principal {identity['Arn']}",
            request_id=_request_id(identity),
        )
    )

    # --- The writer appends, and cannot delete -------------------------------------
    try:
        writer = assume(target.writer_role_arn, "assume writer")
    except (BotoCoreError, ClientError) as exc:
        return failed("assume writer", exc)
    writer_s3 = client(writer, "s3")
    try:
        put = writer_s3.put_object(
            Bucket=target.bucket,
            Key=key,
            Body=body,
            ContentType="application/vnd.apache.parquet",
        )
    except (BotoCoreError, ClientError) as exc:
        return failed("write events", exc)
    steps.append(
        SmokeStep(
            name="write events",
            ok=True,
            detail=f"s3://{target.bucket}/{key}"
            + (f", version {put['VersionId']}" if put.get("VersionId") else "")
            + (f", {put['ServerSideEncryption']}" if put.get("ServerSideEncryption") else ""),
            request_id=_request_id(put),
        )
    )

    try:
        deleted = writer_s3.delete_object(Bucket=target.bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "AccessDenied":
            return failed("writer cannot delete", exc)
        steps.append(
            SmokeStep(
                name="writer cannot delete",
                ok=True,
                detail="DeleteObject refused with AccessDenied, as the writer's Deny requires",
                request_id=_request_id(exc.response),
            )
        )
    except BotoCoreError as exc:
        return failed("writer cannot delete", exc)
    else:
        steps.append(
            SmokeStep(
                name="writer cannot delete",
                ok=False,
                detail="the writer role DELETED an event: the lake's append-only Deny is "
                "missing (invariant 6). The object now carries a delete marker.",
                request_id=_request_id(deleted),
            )
        )
        return report(object_key=key)

    # --- The operator registers the partition ---------------------------------------
    glue = client(session, "glue")
    try:
        table = glue.get_table(DatabaseName=target.database, Name=target.table)
        descriptor = dict(table["Table"]["StorageDescriptor"])
        descriptor["Location"] = target.partition_location
        registered = glue.create_partition(
            DatabaseName=target.database,
            TableName=target.table,
            PartitionInput={"Values": [SMOKE_CONFIG_ID], "StorageDescriptor": descriptor},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code", "") != "AlreadyExistsException":
            return failed("register partition", exc)
        steps.append(
            SmokeStep(
                name="register partition",
                ok=True,
                detail=f"config_id={SMOKE_CONFIG_ID} was already registered",
                request_id=_request_id(exc.response),
            )
        )
    except BotoCoreError as exc:
        return failed("register partition", exc)
    else:
        steps.append(
            SmokeStep(
                name="register partition",
                ok=True,
                detail=f"config_id={SMOKE_CONFIG_ID} at {target.partition_location}",
                request_id=_request_id(registered),
            )
        )

    # --- The analyst queries through the workgroup -----------------------------------
    try:
        analyst = assume(target.analyst_role_arn, "assume analyst")
    except (BotoCoreError, ClientError) as exc:
        return failed("assume analyst", exc)
    athena = client(analyst, "athena")
    try:
        started = athena.start_query_execution(
            QueryString=smoke_query(target),
            QueryExecutionContext={"Database": target.database},
            WorkGroup=target.workgroup,
        )
        execution_id = str(started["QueryExecutionId"])
        state, reason, scanned = "QUEUED", "", None
        for _ in range(_QUERY_POLLS):
            execution = athena.get_query_execution(QueryExecutionId=execution_id)["QueryExecution"]
            state = str(execution["Status"]["State"])
            reason = str(execution["Status"].get("StateChangeReason", ""))
            scanned = execution.get("Statistics", {}).get("DataScannedInBytes")
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            sleep(_QUERY_POLL_SECONDS)
        if state != "SUCCEEDED":
            steps.append(
                SmokeStep(
                    name="query",
                    ok=False,
                    detail=f"Athena query {execution_id} ended {state}: {reason or 'no reason given'}",
                    request_id=_request_id(started),
                )
            )
            return report(object_key=key, query_execution_id=execution_id)
        results = athena.get_query_results(QueryExecutionId=execution_id)
    except (BotoCoreError, ClientError) as exc:
        return failed("query", exc)
    steps.append(
        SmokeStep(
            name="query",
            ok=True,
            detail=f"Athena query {execution_id} in workgroup {target.workgroup}, "
            f"{scanned if scanned is not None else 'unknown'} bytes scanned",
            request_id=_request_id(started),
        )
    )

    # The first row of an Athena result set is the header.
    returned = tuple(
        tuple(cell.get("VarCharValue") for cell in row["Data"])
        for row in results["ResultSet"]["Rows"][1:]
    )
    expected = expected_result_rows(events)
    steps.append(
        SmokeStep(
            name="round trip",
            ok=returned == expected,
            detail=(
                f"{len(returned)} rows returned, equal to the {len(expected)} written"
                if returned == expected
                else f"{len(returned)} rows returned and they differ from the {len(expected)} written"
            ),
            request_id=_request_id(results),
        )
    )
    return report(
        object_key=key,
        query_execution_id=execution_id,
        data_scanned_bytes=int(scanned) if scanned is not None else None,
        rows=returned,
    )

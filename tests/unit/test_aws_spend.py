"""Reading a record of spend that this process did not write (ADR-0048; M15).

The claim this file holds is that `cascade/trace/aws_spend.py` reports one of
three things and never a fourth: a measured token total, *unreachable*, or
*not configured*. A zero is not among them. M8 found that failure once already
-- zero agreed with zero to 0.0000% over 452,328 model calls -- and a second
source that returns zero when it cannot be read would reintroduce it while
looking like extra rigour.

There is no AWS account here, so the standard is verified-against-a-mock:

* The **record shape** is AWS's documented model-invocation log entry, with the
  example record's own token counts (25 in, 150 out), asserted through the pure
  accumulator. Nothing here guesses at field names.
* The **API shape** is exercised through a real `boto3` CloudWatch Logs client
  driven by `botocore.stub.Stubber`, so botocore validates the operation, the
  parameter names and their types against its own service model before any stub
  answers. A hand-built double would accept a call AWS would reject.
* The **routing** is asserted against decoy environment variables, because
  ADR-0028's rule is that a stray `AWS_REGION` must not decide where the
  study's spend is checked.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.stub import Stubber

from cascade.config import ObservabilityConfig, Settings
from cascade.trace.aws_spend import (
    AWS_INVOCATION_LOG_SOURCE,
    MODEL_INVOCATION_SCHEMA_TYPE,
    accumulate,
    invocation_log_reading,
    logs_client,
)

GROUP = "/aws/bedrock/modelinvocations"
REGION = "us-east-1"

# The decoys ADR-0028 names, plus the two endpoint variables botocore reads.
# Any of them winning would point the reconciliation at a different account.
DECOYS = {
    "AWS_REGION": "ap-southeast-2",
    "AWS_DEFAULT_REGION": "ap-southeast-2",
    "AWS_ENDPOINT_URL": "https://decoy.example.com",
    "AWS_ENDPOINT_URL_CLOUDWATCHLOGS": "https://decoy-logs.example.com",
}


def record(
    *,
    input_tokens: int | None = 25,
    output_tokens: int | None = 150,
    model: str = "anthropic.claude-haiku-4-5-20251001-v1:0",
    schema_type: str = MODEL_INVOCATION_SCHEMA_TYPE,
) -> str:
    """One log entry in the shape AWS documents, as a CloudWatch message.

    Defaults are the token counts from AWS's own example record, so the
    arithmetic below is checkable against the documentation rather than against
    this file.
    """
    body: dict[str, Any] = {
        "schemaType": schema_type,
        "schemaVersion": "1.0",
        "timestamp": "2026-09-15T12:00:00Z",
        "accountId": "123456789012",
        "region": REGION,
        "requestId": "abcd1234-5678-efgh-ijkl-mnopqrstuvwx",
        "operation": "Converse",
        "modelId": model,
        "identity": {"arn": "arn:aws:sts::123456789012:assumed-role/Cascade/session"},
        "input": {"inputContentType": "application/json", "inputBodyJson": {}},
        "output": {"outputContentType": "application/json", "outputBodyJson": {}},
    }
    if input_tokens is not None:
        body["input"]["inputTokenCount"] = input_tokens
    if output_tokens is not None:
        body["output"]["outputTokenCount"] = output_tokens
    return json.dumps(body)


def configured(settings: Settings, *, group: str | None, region: str | None) -> Settings:
    return settings.model_copy(
        update={
            "observability": ObservabilityConfig(aws_invocation_log_group=group, aws_region=region)
        }
    )


def page(*messages: str, next_token: str | None = None) -> dict[str, Any]:
    """A FilterLogEvents response, as the service model requires it."""
    body: dict[str, Any] = {
        "events": [
            {
                "logStreamName": "aws/bedrock/modelinvocations",
                "timestamp": 1_757_000_000_000 + index,
                "ingestionTime": 1_757_000_000_001 + index,
                "eventId": str(index),
                "message": message,
            }
            for index, message in enumerate(messages)
        ]
    }
    if next_token is not None:
        body["nextToken"] = next_token
    return body


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake identity, as ADR-0028's provider tests do. Routing is asserted, not faked."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    for name in sorted(DECOYS):
        monkeypatch.setenv(name, DECOYS[name])


class TestTheDocumentedRecord:
    """AWS's log entry format, read rather than assumed."""

    def test_one_record_contributes_its_documented_token_counts(self) -> None:
        totals = accumulate([record()])
        assert totals.records == 1
        assert totals.input_tokens == 25
        assert totals.output_tokens == 150
        assert totals.total_tokens == 175

    def test_records_sum(self) -> None:
        totals = accumulate([record(), record(input_tokens=5, output_tokens=7)])
        assert (totals.records, totals.input_tokens, totals.output_tokens) == (2, 30, 157)

    def test_no_records_is_all_zeros_and_nothing_rejected(self) -> None:
        totals = accumulate([])
        assert (totals.records, totals.total_tokens, totals.rejected) == (0, 0, 0)

    def test_models_are_reported_sorted_and_deduplicated(self) -> None:
        """Sorted because it reaches a report (invariant 7); deduplicated because
        a run makes thousands of calls against a handful of models."""
        totals = accumulate([record(model="zeta"), record(model="alpha"), record(model="zeta")])
        assert totals.models == ("alpha", "zeta")

    def test_the_sum_does_not_depend_on_the_order_pages_arrived_in(self) -> None:
        first = record(input_tokens=1, output_tokens=2)
        second = record(input_tokens=30, output_tokens=40)
        assert accumulate([first, second]) == accumulate([second, first])


class TestWhatTheAccumulatorRefuses:
    """Every rejection is counted. A parser that understood nothing sums to zero
    exactly like a log that recorded nothing, and those are different findings."""

    def test_a_different_schema_is_excluded_and_counted(self) -> None:
        totals = accumulate([record(), record(schema_type="SomethingElse")])
        assert totals.records == 1
        assert totals.foreign_schema == 1
        assert totals.total_tokens == 175  # the foreign record contributed nothing

    def test_unparseable_json_is_counted_not_crashed_on(self) -> None:
        totals = accumulate(["{not json", record()])
        assert (totals.records, totals.malformed) == (1, 1)

    def test_a_json_document_that_is_not_an_object_is_malformed(self) -> None:
        assert accumulate(["[1, 2, 3]"]).malformed == 1

    def test_a_non_numeric_token_count_is_malformed_rather_than_zero(self) -> None:
        """Coercing it to zero would understate the independent record silently,
        which is the direction that makes the meter look correct."""
        broken = json.dumps(
            {
                "schemaType": MODEL_INVOCATION_SCHEMA_TYPE,
                "input": {"inputTokenCount": "many"},
                "output": {"outputTokenCount": 3},
            }
        )
        totals = accumulate([broken])
        assert (totals.records, totals.malformed, totals.total_tokens) == (0, 1, 0)

    def test_a_record_with_no_token_counts_still_counts_as_an_invocation(self) -> None:
        """A failed invocation is an invocation. Dropping it would understate the
        call count against a ledger that counted the call."""
        totals = accumulate([record(input_tokens=None, output_tokens=None)])
        assert (totals.records, totals.without_tokens, totals.total_tokens) == (1, 1, 0)

    def test_a_record_whose_input_is_not_an_object_is_malformed(self) -> None:
        broken = json.dumps({"schemaType": MODEL_INVOCATION_SCHEMA_TYPE, "input": "elsewhere"})
        assert accumulate([broken]).malformed == 1


class TestRoutingIsExplicit:
    """ADR-0028: routing comes from configuration, identity from the environment."""

    def test_the_configured_region_beats_every_ambient_decoy(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        client = logs_client(configured(settings, group=GROUP, region=REGION))
        assert client.meta.region_name == REGION
        assert DECOYS["AWS_REGION"] not in str(client.meta.endpoint_url)

    def test_the_endpoint_is_the_regional_one_despite_a_configured_endpoint_url(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        """Without `ignore_configured_endpoint_urls`, `AWS_ENDPOINT_URL` decides
        which service answers "was the study's spend real"."""
        client = logs_client(configured(settings, group=GROUP, region=REGION))
        assert client.meta.endpoint_url == f"https://logs.{REGION}.amazonaws.com"

    def test_a_missing_region_raises_rather_than_falling_back(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        with pytest.raises(ValueError, match=r"observability\.aws_region"):
            logs_client(configured(settings, group=GROUP, region=None))


class TestReadingTheLogGroup:
    """Driven through the real client, so botocore validates the call shape."""

    def test_a_single_page_is_read_and_totalled(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        live = configured(settings, group=GROUP, region=REGION)
        client = logs_client(live)
        with Stubber(client) as stub:
            stub.add_response("filter_log_events", page(record()), {"logGroupName": GROUP})
            reading = invocation_log_reading(live, client=client)
            stub.assert_no_pending_responses()

        assert reading.status == "read"
        assert reading.name == AWS_INVOCATION_LOG_SOURCE
        assert reading.spend is not None
        assert (reading.spend.input_tokens, reading.spend.output_tokens) == (25, 150)
        assert reading.spend.calls == 1

    def test_every_page_is_read(self, settings: Settings, aws_credentials: None) -> None:
        """A study's invocations do not fit one page, and a reader that stopped
        at the first would report a fraction of the record as the whole of it --
        a discrepancy blamed on the meter."""
        live = configured(settings, group=GROUP, region=REGION)
        client = logs_client(live)
        with Stubber(client) as stub:
            stub.add_response(
                "filter_log_events",
                page(record(), next_token="page-2"),
                {"logGroupName": GROUP},
            )
            stub.add_response(
                "filter_log_events",
                page(record(input_tokens=1, output_tokens=2), next_token="page-3"),
                {"logGroupName": GROUP, "nextToken": "page-2"},
            )
            stub.add_response(
                "filter_log_events",
                page(record(input_tokens=4, output_tokens=8)),
                {"logGroupName": GROUP, "nextToken": "page-3"},
            )
            reading = invocation_log_reading(live, client=client)
            stub.assert_no_pending_responses()

        assert reading.spend is not None
        assert reading.spend.calls == 3
        assert (reading.spend.input_tokens, reading.spend.output_tokens) == (30, 160)

    def test_the_record_carries_tokens_and_states_no_price(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        """Pricing AWS's tokens from the meter's own table would compare the
        price table with itself and agree by construction (ADR-0048)."""
        live = configured(settings, group=GROUP, region=REGION)
        client = logs_client(live)
        with Stubber(client) as stub:
            stub.add_response("filter_log_events", page(record()), {"logGroupName": GROUP})
            reading = invocation_log_reading(live, client=client)

        assert reading.basis == "tokens"
        assert reading.spend is not None
        assert reading.spend.total_usd is None

    def test_the_record_is_marked_as_written_by_aws(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        """The one property that makes this source worth reading at all."""
        live = configured(settings, group=GROUP, region=REGION)
        client = logs_client(live)
        with Stubber(client) as stub:
            stub.add_response("filter_log_events", page(record()), {"logGroupName": GROUP})
            reading = invocation_log_reading(live, client=client)

        assert reading.written_by == "aws"
        assert reading.independent

    def test_a_window_is_passed_as_epoch_milliseconds(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        live = configured(settings, group=GROUP, region=REGION)
        client = logs_client(live)
        start = datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime(2026, 9, 2, tzinfo=UTC)
        with Stubber(client) as stub:
            stub.add_response(
                "filter_log_events",
                page(record()),
                {
                    "logGroupName": GROUP,
                    "startTime": 1_788_220_800_000,  # 2026-09-01T00:00:00Z
                    "endTime": 1_788_307_200_000,  # 2026-09-02T00:00:00Z
                },
            )
            invocation_log_reading(live, client=client, start=start, end=end)
            stub.assert_no_pending_responses()

    def test_a_naive_window_bound_is_refused(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        """`timestamp()` reads a naive datetime as local time, which would scope
        the independent record to hours the ledger does not cover -- and the
        shift would be the machine's, so it would reproduce nowhere else.

        It raises rather than reporting *unreachable*: the caller asked an
        ill-formed question and AWS was never asked at all. Reporting a caller's
        bug as an outage is the wrong diagnosis, which is the M8 lesson about a
        harness failure recorded as a replay divergence.
        """
        live = configured(settings, group=GROUP, region=REGION)
        client = logs_client(live)
        with pytest.raises(ValueError, match="naive"):
            invocation_log_reading(live, client=client, start=datetime(2026, 9, 1))


class TestAbsenceIsNeverZero:
    """The single most important behaviour here, tested from every direction."""

    def test_no_log_group_is_not_configured_and_does_not_block(self, settings: Settings) -> None:
        """A deployment with no AWS account still has §12.4's gate against
        Langfuse; this source simply was not asked, and says so."""
        reading = invocation_log_reading(configured(settings, group=None, region=REGION))
        assert reading.status == "not configured"
        assert reading.spend is None
        assert "aws_invocation_log_group" in reading.note

    def test_a_log_group_without_a_region_is_unreachable_not_unconfigured(
        self, settings: Settings
    ) -> None:
        """Someone who meant to add the second record and mistyped one field must
        not read a green gate that checked one fewer source than they believe."""
        reading = invocation_log_reading(configured(settings, group=GROUP, region=None))
        assert reading.status == "unreachable"
        assert reading.spend is None
        assert "aws_region" in reading.note

    def test_a_log_group_that_does_not_exist_is_unreachable(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        live = configured(settings, group=GROUP, region=REGION)
        client = logs_client(live)
        with Stubber(client) as stub:
            stub.add_client_error(
                "filter_log_events",
                service_error_code="ResourceNotFoundException",
                service_message="The specified log group does not exist.",
            )
            reading = invocation_log_reading(live, client=client)

        assert reading.status == "unreachable"
        assert reading.spend is None
        assert "ResourceNotFoundException" in reading.note

    def test_a_service_outage_is_unreachable_and_names_itself(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        live = configured(settings, group=GROUP, region=REGION)
        client = logs_client(live)
        with Stubber(client) as stub:
            stub.add_client_error(
                "filter_log_events",
                service_error_code="ServiceUnavailableException",
                http_status_code=503,
            )
            reading = invocation_log_reading(live, client=client)

        assert reading.status == "unreachable"
        assert "ServiceUnavailable" in reading.note

    def test_a_missing_boto3_is_unreachable_rather_than_an_import_error(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`boto3` ships in the optional `aws` extra. A checkout without it must
        still run §12.4's gate and report this source as unread."""
        import builtins

        real_import = builtins.__import__

        def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.split(".")[0] in {"boto3", "botocore"}:
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        reading = invocation_log_reading(configured(settings, group=GROUP, region=REGION))

        assert reading.status == "unreachable"
        assert reading.spend is None
        assert "ImportError" in reading.note

    @pytest.mark.parametrize("group,region", [(None, None), (None, REGION), (GROUP, None)])
    def test_no_unread_source_ever_reports_a_total(
        self, settings: Settings, group: str | None, region: str | None
    ) -> None:
        reading = invocation_log_reading(configured(settings, group=group, region=region))
        assert reading.spend is None

    def test_the_guard_above_is_not_vacuous(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        """Guard the guard: if nothing ever produced a total, every assertion in
        this class would hold over a module that does not work."""
        live = configured(settings, group=GROUP, region=REGION)
        client = logs_client(live)
        with Stubber(client) as stub:
            stub.add_response("filter_log_events", page(record()), {"logGroupName": GROUP})
            reading = invocation_log_reading(live, client=client)
        assert reading.spend is not None


class TestAnEmptyLogGroupIsAReadingNotAnAbsence:
    def test_it_is_read_with_nothing_in_it_and_says_what_to_check(
        self, settings: Settings, aws_credentials: None
    ) -> None:
        """AWS answered, and the answer was "no invocations". That is a
        measurement -- the reconciliation refuses it as vacuous or as a total
        disagreement -- so the job here is to name the likely cause. Bedrock
        documents that invocation logging covers the `bedrock-runtime` endpoint
        only, and this project's `bedrock` provider is routed at
        `bedrock-mantle` (ADR-0028), which it does not cover."""
        live = configured(settings, group=GROUP, region=REGION)
        client = logs_client(live)
        with Stubber(client) as stub:
            stub.add_response("filter_log_events", page(), {"logGroupName": GROUP})
            reading = invocation_log_reading(live, client=client)

        assert reading.status == "read"
        assert reading.spend is not None
        assert reading.spend.calls == 0
        assert "bedrock-runtime" in reading.spend.detail

"""`cascade aws check`: the AWS surfaces, read without spending (ADR-0053).

Three control-plane reads -- the caller's identity, the configured guardrail,
the configured reranker -- and nothing that invokes a model. Driven through a
fake boto3 session so no test needs an account; the one thing asserted about
routing is the thing ADR-0028 cares about: every client is built for the
configured region, never for an ambient one.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from cascade.cli import app
from cascade.config import Settings
from cascade.llm.client import AwsAccessReport, AwsCheck, describe_aws_access
from cascade.version import EXIT_OK, EXIT_PRECONDITION

runner = CliRunner()


def client_error(code: str, operation: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": f"{code} on {operation}"}}, operation)


class FakeClient:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, operation: str) -> Any:
        if operation not in self.answers:
            raise AttributeError(operation)

        def call(**kwargs: Any) -> Any:
            self.calls.append((operation, kwargs))
            answer = self.answers[operation]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return call


class FakeSession:
    """Hands out one fake client per service and remembers the region asked for."""

    def __init__(self, **clients: FakeClient) -> None:
        self.clients = clients
        self.regions: dict[str, str | None] = {}

    def client(self, service: str, *, region_name: str | None = None, **_: Any) -> FakeClient:
        self.regions[service] = region_name
        return self.clients[service]


def configured(settings: Settings, **bedrock: Any) -> Settings:
    section = settings.providers.bedrock.model_copy(update=bedrock)
    return settings.model_copy(
        update={"providers": settings.providers.model_copy(update={"bedrock": section})}
    )


def with_bedrock_rerank(settings: Settings) -> Settings:
    rerank = settings.retrieval.rerank.model_copy(
        update={"enabled": True, "provider": "bedrock", "model_id": "amazon.rerank-v1:0"}
    )
    return settings.model_copy(
        update={"retrieval": settings.retrieval.model_copy(update={"rerank": rerank})}
    )


IDENTITY = {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/cascade"}


def test_no_region_is_the_only_finding_and_nothing_is_asked(settings: Settings) -> None:
    session = FakeSession()
    report = describe_aws_access(configured(settings, region=None), session=session)
    assert not report.ok
    assert [check.name for check in report.checks] == ["region"]
    assert session.regions == {}


def test_every_check_passes_and_every_client_is_built_for_the_configured_region(
    settings: Settings,
) -> None:
    pytest.importorskip("botocore")
    session = FakeSession(
        sts=FakeClient({"get_caller_identity": IDENTITY}),
        bedrock=FakeClient(
            {
                "get_guardrail": {
                    "name": "cascade-audit-guardrail",
                    "version": "1",
                    "status": "READY",
                },
                "get_foundation_model": {
                    "modelDetails": {
                        "modelName": "Rerank 1.0",
                        "modelLifecycle": {"status": "ACTIVE"},
                    }
                },
            }
        ),
    )
    ready = with_bedrock_rerank(
        configured(settings, guardrail_id="gr-abc123", guardrail_version="1")
    )
    report = describe_aws_access(ready, session=session)

    assert report.ok, [c for c in report.checks if not c.ok]
    assert [check.name for check in report.checks] == [
        "region",
        "identity",
        "guardrail",
        "rerank model",
    ]
    assert report.region == "us-west-2"
    assert session.regions == {"sts": "us-west-2", "bedrock": "us-west-2"}
    guardrail_call = session.clients["bedrock"].calls[0]
    assert guardrail_call == (
        "get_guardrail",
        {"guardrailIdentifier": "gr-abc123", "guardrailVersion": "1"},
    )
    assert session.clients["bedrock"].calls[1] == (
        "get_foundation_model",
        {"modelIdentifier": "amazon.rerank-v1:0"},
    )
    by_name = {check.name: check for check in report.checks}
    assert "cascade-audit-guardrail" in by_name["guardrail"].detail
    assert "Rerank 1.0" in by_name["rerank model"].detail
    assert "123456789012" in by_name["identity"].detail


def test_an_unconfigured_guardrail_and_a_local_reranker_are_reported_not_failed(
    settings: Settings,
) -> None:
    pytest.importorskip("botocore")
    session = FakeSession(sts=FakeClient({"get_caller_identity": IDENTITY}), bedrock=FakeClient({}))
    report = describe_aws_access(settings, session=session)
    assert report.ok
    by_name = {check.name: check for check in report.checks}
    assert "not configured" in by_name["guardrail"].detail
    assert "local reranker" in by_name["rerank model"].detail
    assert session.clients["bedrock"].calls == []


def test_no_credentials_fails_the_identity_check_and_stops_there(settings: Settings) -> None:
    from botocore.exceptions import NoCredentialsError

    session = FakeSession(
        sts=FakeClient({"get_caller_identity": NoCredentialsError()}),
        bedrock=FakeClient({}),
    )
    report = describe_aws_access(
        configured(settings, guardrail_id="gr-abc123", guardrail_version="1"), session=session
    )
    assert not report.ok
    assert [check.name for check in report.checks] == ["region", "identity"]
    assert "NoCredentialsError" in report.checks[-1].detail


def test_a_guardrail_that_does_not_resolve_fails_its_own_check_only(settings: Settings) -> None:
    pytest.importorskip("botocore")
    session = FakeSession(
        sts=FakeClient({"get_caller_identity": IDENTITY}),
        bedrock=FakeClient(
            {"get_guardrail": client_error("ResourceNotFoundException", "GetGuardrail")}
        ),
    )
    report = describe_aws_access(
        configured(settings, guardrail_id="gr-missing", guardrail_version="1"), session=session
    )
    by_name = {check.name: check for check in report.checks}
    assert not report.ok
    assert (
        not by_name["guardrail"].ok and "ResourceNotFoundException" in by_name["guardrail"].detail
    )
    assert by_name["identity"].ok and by_name["rerank model"].ok


def test_the_command_exits_three_when_a_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import cascade.llm.client as client_module

    failing = AwsAccessReport(
        region="us-west-2",
        checks=(
            AwsCheck("region", True, "us-west-2"),
            AwsCheck("identity", False, "NoCredentialsError: Unable to locate credentials"),
        ),
    )
    monkeypatch.setattr(client_module, "describe_aws_access", lambda settings: failing)
    result = runner.invoke(app, ["aws", "check"])
    assert result.exit_code == EXIT_PRECONDITION
    assert "NoCredentialsError" in result.output


def test_the_command_exits_zero_when_every_check_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    import cascade.llm.client as client_module

    passing = AwsAccessReport(
        region="us-west-2",
        checks=(
            AwsCheck("region", True, "us-west-2"),
            AwsCheck("identity", True, "account 123456789012, principal arn:aws:iam::1:user/x"),
            AwsCheck("guardrail", True, "cascade-audit-guardrail (gr-1 version 1), status READY"),
            AwsCheck("rerank model", True, "local reranker (bm25-local-v1)"),
        ),
    )
    monkeypatch.setattr(client_module, "describe_aws_access", lambda settings: passing)
    result = runner.invoke(app, ["aws", "check"])
    assert result.exit_code == EXIT_OK
    assert "cascade-audit-guardrail" in result.output


def test_the_check_reaches_only_control_plane_services() -> None:
    """A read-only command must build no runtime client: nothing here can invoke a model."""
    import inspect

    import cascade.llm.client as client_module

    source = inspect.getsource(client_module.describe_aws_access)
    assert 'client("sts"' in source and 'client("bedrock"' in source
    assert 'client("bedrock-runtime"' not in source
    assert 'client("bedrock-agent-runtime"' not in source
    for operation in ("invoke_model", "apply_guardrail", "rerank(", "messages.create"):
        assert operation not in source

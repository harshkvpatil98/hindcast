"""The provider seam (ADR-0028, ADR-0029, ADR-0031).

Four providers sit behind the one call site. What these tests pin down is
that switching between them changes where a call goes and how it is billed --
and never which recording it resolves to, except for the one provider that
cannot serve the request its key describes.

The AWS providers are driven through the *real* SDK against a mock transport,
with fake credentials in the environment, so the SigV4 signature, the endpoint
and the wire model id are the SDK's own. The CLI provider is driven through an
injected runner: no test here runs `claude` or spends a subscription's
allowance.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from cascade.config import PricingEntry, Settings, claude_cli_environment
from cascade.llm.cache import cache_key
from cascade.llm.claude_cli import (
    ISOLATION_FLAGS,
    UnsupportedRequestShape,
    build_invocation,
    to_messages_payload,
)
from cascade.llm.client import CliCompleted, LLMClient
from cascade.llm.providers import (
    PROVIDERS,
    endpoint,
    readiness_problems,
    render_model,
)
from cascade.llm.types import BatchItem, CacheMiss, LLMError, LLMRequest, ProviderNotReady
from tests.conftest import NetworkForbidden, message_payload

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

# Deliberately unlike the first-party rates, so a call priced from the wrong
# table produces a visibly wrong number rather than a coincidentally right one.
PARTNER_PRICES = {
    HAIKU: PricingEntry(input_per_mtok=Decimal("2.00"), output_per_mtok=Decimal("10.00")),
    SONNET: PricingEntry(input_per_mtok=Decimal("6.00"), output_per_mtok=Decimal("30.00")),
}


def request_for(index: int = 0, *, model: str = HAIKU) -> LLMRequest:
    return LLMRequest(
        model=model,
        system="rules",
        messages=[{"role": "user", "content": f"turn {index}"}],
        temperature=0.7,
        max_tokens=512,
        prompt_rev="r2",
    )


def on(settings: Settings, provider: str, *, mode: str = "record", **section: Any) -> Settings:
    """``settings`` switched to ``provider``, with its section updated."""
    providers = settings.providers
    if section:
        current = getattr(providers, provider)
        providers = providers.model_copy(update={provider: current.model_copy(update=section)})
    return settings.model_copy(
        update={
            "llm": settings.llm.model_copy(update={"provider": provider, "mode": mode}),
            "providers": providers,
        }
    )


def ready_bedrock(settings: Settings, **extra: Any) -> Settings:
    return on(settings, "bedrock", region="us-east-1", pricing=PARTNER_PRICES, **extra)


def ready_aws(settings: Settings, **extra: Any) -> Settings:
    return on(
        settings,
        "aws",
        region="us-east-1",
        workspace_id="wrkspc_test",
        pricing=PARTNER_PRICES,
        **extra,
    )


@pytest.fixture
def aws_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fake AWS credentials, plus decoys for every ambient routing variable.

    The credentials let botocore compute a real SigV4 signature offline. The
    decoys are what the explicit-routing tests exist to defeat: each one is a
    variable the SDK would consult if the client left a routing value unset.
    """
    pytest.importorskip("botocore")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent-aws-credentials"))
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.setenv("ANTHROPIC_AWS_BASE_URL", "https://decoy.invalid")
    monkeypatch.setenv("ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "https://decoy.invalid")
    monkeypatch.setenv("ANTHROPIC_AWS_WORKSPACE_ID", "wrkspc_decoy")


class Captured:
    """A transport that answers /v1/messages and keeps every request it saw."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            body = json.loads(request.content)
            return httpx.Response(200, json=message_payload(model=body["model"]))

        return httpx.Client(transport=httpx.MockTransport(handler))


def forbidding() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise NetworkForbidden(f"unexpected network call to {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# The static description
# ---------------------------------------------------------------------------


def test_only_the_cli_has_a_cache_namespace_of_its_own() -> None:
    """The three API providers share one namespace (ADR-0029); the CLI cannot."""
    assert {name: spec.cache_namespace for name, spec in sorted(PROVIDERS.items())} == {
        "anthropic": None,
        "aws": None,
        "bedrock": None,
        "claude_code": "claude_code",
    }


def test_only_anthropic_and_aws_can_carry_a_batched_phase() -> None:
    assert sorted(name for name, spec in PROVIDERS.items() if spec.supports_batches) == [
        "anthropic",
        "aws",
    ]


def test_the_wire_id_is_logical_except_on_bedrock(settings: Settings) -> None:
    for provider in ("anthropic", "aws", "claude_code"):
        assert render_model(on(settings, provider), HAIKU) == HAIKU
    assert render_model(on(settings, "bedrock"), HAIKU) == f"anthropic.{HAIKU}"


def test_a_bedrock_model_override_wins_over_the_derived_id(settings: Settings) -> None:
    configured = on(settings, "bedrock", model_ids={HAIKU: "us.anthropic.haiku-profile"})
    assert render_model(configured, HAIKU) == "us.anthropic.haiku-profile"
    assert render_model(configured, SONNET) == f"anthropic.{SONNET}"


# ---------------------------------------------------------------------------
# Readiness: refuse before the first dollar
# ---------------------------------------------------------------------------


def test_an_unconfigured_provider_reports_every_problem_at_once(settings: Settings) -> None:
    problems = readiness_problems(on(settings, "bedrock"), models=(HAIKU, SONNET))
    assert problems == sorted(problems)
    assert "providers.bedrock.region is not set" in problems
    assert sum("providers.bedrock.pricing has no entry" in p for p in problems) == 2
    assert all("aws.amazon.com/bedrock/pricing" in p for p in problems if "pricing" in p)


def test_claude_platform_on_aws_needs_a_workspace(settings: Settings) -> None:
    problems = readiness_problems(on(settings, "aws", region="us-east-1"), models=(HAIKU,))
    assert "providers.aws.workspace_id is not set" in problems


def test_a_subscription_needs_no_price_table_and_books_zero(settings: Settings) -> None:
    cli = on(settings, "claude_code")
    assert readiness_problems(cli, models=(HAIKU, SONNET)) == []
    assert cli.price_for(HAIKU) == PricingEntry(
        input_per_mtok=Decimal(0), output_per_mtok=Decimal(0)
    )


def test_record_mode_refuses_an_unready_provider_at_construction(settings: Settings) -> None:
    transport = Captured()
    with pytest.raises(ProviderNotReady, match="region is not set"):
        LLMClient(on(settings, "bedrock"), phase="compile", http_client=transport.client())
    assert transport.requests == []


def test_replay_needs_no_provider_configuration(settings: Settings) -> None:
    """Replay never reaches a provider, which is what keeps `make demo` keyless."""
    client = LLMClient(
        on(settings, "bedrock", mode="replay"), phase="compile", http_client=forbidding()
    )
    with pytest.raises(CacheMiss):
        client.complete(request_for())
    assert not client.constructed_sdk_client


# ---------------------------------------------------------------------------
# The AWS providers, through the real SDK
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("aws_identity")
def test_bedrock_is_signed_routed_explicitly_and_priced_from_its_own_table(
    settings: Settings,
) -> None:
    transport = Captured()
    client = LLMClient(ready_bedrock(settings), phase="compile", http_client=transport.client())
    client.complete(request_for())

    (sent,) = transport.requests
    # Settings won over the ambient AWS_REGION and base-URL decoys.
    assert sent.url.host == "bedrock-mantle.us-east-1.api.aws"
    assert sent.url.path.endswith("/v1/messages")
    assert json.loads(sent.content)["model"] == f"anthropic.{HAIKU}"
    authorization = sent.headers["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock-mantle/aws4_request" in authorization
    # Priced by the logical model from Bedrock's own table: the response echoes
    # the prefixed id, which is in no table, so pricing by the response would
    # have raised rather than booked the wrong number.
    assert client.meter.total_usd == (Decimal(1900) * 2 + Decimal(45) * 10) / Decimal(1_000_000)


@pytest.mark.usefixtures("aws_identity")
def test_claude_platform_on_aws_carries_its_workspace_and_the_bare_model_id(
    settings: Settings,
) -> None:
    transport = Captured()
    client = LLMClient(ready_aws(settings), phase="compile", http_client=transport.client())
    client.complete(request_for())

    (sent,) = transport.requests
    assert sent.url.host == "aws-external-anthropic.us-east-1.api.aws"
    assert sent.headers["anthropic-workspace-id"] == "wrkspc_test"
    assert json.loads(sent.content)["model"] == HAIKU
    assert "/us-east-1/aws-external-anthropic/aws4_request" in sent.headers["authorization"]


def test_the_endpoint_templates_agree_with_the_installed_sdk(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy in providers.py cannot drift from what the SDK itself derives."""
    anthropic = pytest.importorskip("anthropic")
    pytest.importorskip("botocore")
    monkeypatch.delenv("ANTHROPIC_AWS_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BEDROCK_MANTLE_BASE_URL", raising=False)
    creds = {"aws_access_key": "AKIDEXAMPLE", "aws_secret_key": "secret"}

    sdk_aws = anthropic.AnthropicAWS(aws_region="us-east-1", workspace_id="w", **creds)
    sdk_bedrock = anthropic.AnthropicBedrockMantle(aws_region="us-east-1", **creds)

    ours_aws = endpoint(on(settings, "aws", region="us-east-1"))
    ours_bedrock = endpoint(on(settings, "bedrock", region="us-east-1"))
    assert ours_aws is not None and ours_bedrock is not None
    assert str(sdk_aws.base_url).rstrip("/") == ours_aws.rstrip("/")
    assert str(sdk_bedrock.base_url).rstrip("/") == ours_bedrock.rstrip("/")


# ---------------------------------------------------------------------------
# One cache, shared by the API providers (ADR-0029)
# ---------------------------------------------------------------------------


def test_existing_recordings_keep_the_key_they_were_recorded_under() -> None:
    """Pinned digest, computed with the pre-ADR-0028 formula.

    If this changes, every recording made before the provider seam became a
    miss -- a paid re-record of the whole study, discovered only in replay.
    """
    assert cache_key(request_for()) == (
        "19a385918f2705920bd3194fea6f9eacb592639564607c6dcc5148a02bd60725"
    )
    assert cache_key(request_for(), namespace="claude_code") != cache_key(request_for())


@pytest.mark.usefixtures("aws_identity")
def test_a_recording_made_through_anthropic_replays_through_bedrock(
    settings: Settings,
) -> None:
    transport = Captured()
    recorder = LLMClient(on(settings, "anthropic"), phase="compile", http_client=transport.client())
    recorder.complete(request_for())
    assert len(transport.requests) == 1

    replay = LLMClient(
        on(settings, "bedrock", mode="replay", region="us-east-1", pricing=PARTNER_PRICES),
        phase="compile",
        http_client=forbidding(),
    )
    result = replay.complete(request_for())
    assert result.served_from_cache
    assert not replay.constructed_sdk_client


def test_bedrock_refuses_a_batch_with_misses_before_any_spend(settings: Settings) -> None:
    transport = Captured()
    client = LLMClient(ready_bedrock(settings), phase="simulate", http_client=transport.client())
    items = [BatchItem(custom_id=f"c{i}", request=request_for(i)) for i in range(2)]
    with pytest.raises(ProviderNotReady, match="Message Batches API"):
        client.complete_batch(items)
    assert transport.requests == []
    assert client.meter.total_usd == 0


def test_bedrock_serves_a_wave_that_is_already_recorded(settings: Settings) -> None:
    """Replaying a batched phase through Bedrock costs nothing, so it is allowed."""
    transport = Captured()
    recorder = LLMClient(
        on(settings, "anthropic"), phase="simulate", http_client=transport.client()
    )
    for i in range(2):
        recorder.complete(request_for(i))

    client = LLMClient(ready_bedrock(settings), phase="simulate", http_client=forbidding())
    items = [BatchItem(custom_id=f"c{i}", request=request_for(i)) for i in range(2)]
    served = client.complete_batch(items)
    assert sorted(served) == ["c0", "c1"]
    assert all(result.served_from_cache for result in served.values())


# ---------------------------------------------------------------------------
# The CLI adapter, pure (ADR-0031)
# ---------------------------------------------------------------------------


def forced_request() -> LLMRequest:
    schema = {"type": "object", "properties": {"p": {"type": "number"}}, "required": ["p"]}
    return LLMRequest(
        model=HAIKU,
        system=[{"type": "text", "text": "RULES"}, {"type": "text", "text": "PERSONA"}],
        messages=[{"role": "user", "content": "observe"}],
        tools=[{"name": "act", "description": "d", "input_schema": schema}],
        tool_choice={"type": "tool", "name": "act"},
        temperature=0.7,
        max_tokens=512,
        prompt_rev="r2",
    )


def test_a_plain_request_becomes_an_isolated_invocation() -> None:
    invocation = build_invocation(request_for(), executable="claude", model=HAIKU)
    argv = list(invocation.argv)
    assert argv[:4] == ["claude", "-p", "--model", HAIKU]
    assert argv[argv.index("--system-prompt") + 1] == "rules"
    assert argv[-len(ISOLATION_FLAGS) :] == list(ISOLATION_FLAGS)
    assert "--json-schema" not in argv
    assert invocation.stdin == "turn 0"
    assert invocation.forced_tool is None


def test_a_forced_tool_becomes_structured_output_and_system_blocks_are_joined() -> None:
    invocation = build_invocation(forced_request(), executable="claude", model=HAIKU)
    argv = list(invocation.argv)
    assert argv[argv.index("--system-prompt") + 1] == "RULES\n\nPERSONA"
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["required"] == ["p"]
    assert invocation.forced_tool == "act"


@pytest.mark.parametrize(
    "update",
    [
        {"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]},
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "a"}]}]},
        {"tool_choice": {"type": "auto"}},
        {"tool_choice": {"type": "tool", "name": "other"}},
    ],
)
def test_a_shape_the_cli_cannot_express_is_refused_not_flattened(update: dict[str, Any]) -> None:
    with pytest.raises(UnsupportedRequestShape):
        build_invocation(
            forced_request().model_copy(update=update), executable="claude", model=HAIKU
        )


def cli_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "result": '{"p": 0.6}',
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 448,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "total_cost_usd": 0.001718,
        "num_turns": 1,
        "duration_api_ms": 3309,
        "uuid": "u-1",
    }
    body.update(overrides)
    return body


def test_a_text_result_becomes_a_messages_body_that_states_how_it_was_made() -> None:
    invocation = build_invocation(request_for(), executable="claude", model=HAIKU)
    payload = to_messages_payload(cli_body(), invocation=invocation, logical_model=HAIKU)
    assert payload["content"] == [{"type": "text", "text": '{"p": 0.6}'}]
    assert payload["model"] == HAIKU
    assert payload["usage"]["input_tokens"] == 448
    assert payload["claude_code"]["notional_cost_usd"] == 0.001718


def test_structured_output_returns_as_the_tool_call_the_caller_parses() -> None:
    invocation = build_invocation(forced_request(), executable="claude", model=HAIKU)
    payload = to_messages_payload(
        cli_body(structured_output={"p": 0.45}), invocation=invocation, logical_model=HAIKU
    )
    (block,) = payload["content"]
    assert block["type"] == "tool_use" and block["name"] == "act" and block["input"] == {"p": 0.45}
    assert payload["stop_reason"] == "tool_use"


def test_an_error_or_a_missing_structured_output_is_raised() -> None:
    forced = build_invocation(forced_request(), executable="claude", model=HAIKU)
    with pytest.raises(LLMError, match="reported an error"):
        to_messages_payload(cli_body(is_error=True), invocation=forced, logical_model=HAIKU)
    with pytest.raises(LLMError, match="returned none"):
        to_messages_payload(cli_body(), invocation=forced, logical_model=HAIKU)


# ---------------------------------------------------------------------------
# The CLI through the one door
# ---------------------------------------------------------------------------


class FakeCli:
    """Stands in for `claude -p`: canned results, and a record of every call."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[tuple[str, ...], str, Path]] = []

    def __call__(self, argv: Any, stdin: str, workdir: Path, timeout_s: float) -> CliCompleted:
        self.calls.append((tuple(argv), stdin, workdir))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return CliCompleted(0, json.dumps(outcome), "")


@pytest.fixture
def cli_settings(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The CLI provider, in a clean working directory, with no user memory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return on(settings, "claude_code", workdir=str(tmp_path / "cli-work"))


def test_a_cli_call_is_recorded_booked_at_zero_and_never_repeated(cli_settings: Settings) -> None:
    runner = FakeCli(cli_body())
    client = LLMClient(cli_settings, phase="bench", cli_runner=runner)

    first = client.complete(request_for())
    second = client.complete(request_for())

    assert first.text == '{"p": 0.6}' and not first.served_from_cache
    assert second.served_from_cache
    assert len(runner.calls) == 1
    assert client.meter.total_usd == 0
    assert client.meter.usage.input_tokens == 448
    assert not client.constructed_sdk_client


def test_a_cli_recording_is_never_served_as_an_api_recording(cli_settings: Settings) -> None:
    """The pay-as-you-go switch records afresh; nothing from the CLI leaks in."""
    LLMClient(cli_settings, phase="bench", cli_runner=FakeCli(cli_body())).complete(request_for())

    api_replay = cli_settings.model_copy(
        update={
            "llm": cli_settings.llm.model_copy(update={"provider": "anthropic", "mode": "replay"})
        }
    )
    with pytest.raises(CacheMiss):
        LLMClient(api_replay, phase="bench").complete(request_for())

    cli_replay = cli_settings.model_copy(
        update={"llm": cli_settings.llm.model_copy(update={"mode": "replay"})}
    )
    assert LLMClient(cli_replay, phase="bench").complete(request_for()).served_from_cache


def test_a_forced_tool_reaches_the_caller_as_a_tool_call(cli_settings: Settings) -> None:
    runner = FakeCli(cli_body(structured_output={"p": 0.45}))
    result = LLMClient(cli_settings, phase="bench", cli_runner=runner).complete(forced_request())
    assert result.tool_calls[0]["input"] == {"p": 0.45}


def test_a_usage_limit_stops_at_once_instead_of_retrying(cli_settings: Settings) -> None:
    runner = FakeCli(cli_body(is_error=True, api_error_status=429))
    with pytest.raises(LLMError, match="usage limit"):
        LLMClient(cli_settings, phase="bench", cli_runner=runner).complete(request_for())
    assert len(runner.calls) == 1


def test_a_transient_status_is_retried(
    cli_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LLMClient, "_sleep", lambda self, seconds: None)
    runner = FakeCli(cli_body(is_error=True, api_error_status=529), cli_body())
    result = LLMClient(cli_settings, phase="bench", cli_runner=runner).complete(request_for())
    assert result.text == '{"p": 0.6}'
    assert len(runner.calls) == 2


def test_exhausted_structured_output_retries_are_retried_not_fatal(
    cli_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI-side parse failure carries no HTTP status, so it matches on subtype.

    Measured in the M15 fan-out: this killed a whole scenario worker and with
    it every replicate it had not yet run -- three of thirty-six scenarios,
    ten replicates each, about fifteen minutes in. It is transient in the same
    sense as a 529: the model failed to emit a parseable tool call that time,
    and a fresh call may succeed. It reaches the retry loop with
    `api_error_status=None`, so neither the 429 branch nor
    `_CLI_TRANSIENT_STATUSES` can see it.
    """
    monkeypatch.setattr(LLMClient, "_sleep", lambda self, seconds: None)
    runner = FakeCli(
        cli_body(
            is_error=True,
            api_error_status=None,
            subtype="error_max_structured_output_retries",
            result="",
        ),
        cli_body(),
    )
    result = LLMClient(cli_settings, phase="bench", cli_runner=runner).complete(request_for())
    assert result.text == '{"p": 0.6}'
    assert len(runner.calls) == 2


def test_an_unknown_cli_error_subtype_is_still_fatal(cli_settings: Settings) -> None:
    """The retry set is a whitelist: an unrecognised failure must not loop silently.

    A blanket retry on `is_error` would turn a permanent misconfiguration into
    an unbounded loop that spends the subscription and reports nothing.
    """
    runner = FakeCli(
        cli_body(is_error=True, api_error_status=None, subtype="error_during_execution")
    )
    with pytest.raises(LLMError, match="reported an error"):
        LLMClient(cli_settings, phase="bench", cli_runner=runner).complete(request_for())
    assert len(runner.calls) == 1


def test_a_missing_executable_is_a_precondition(cli_settings: Settings) -> None:
    runner = FakeCli(FileNotFoundError("claude"))
    with pytest.raises(ProviderNotReady, match="not on PATH"):
        LLMClient(cli_settings, phase="bench", cli_runner=runner).complete(request_for())


def test_an_unexecutable_binary_is_a_precondition_not_a_traceback(
    cli_settings: Settings,
) -> None:
    """A CLI that exists but cannot run must reach the same diagnostic.

    Measured during the M15 fan-out: a failed Claude Code auto-update left a
    stub at the install path. `FileNotFoundError` is only the *missing* case,
    so every worker raised ENOEXEC as a sixty-line traceback -- sixteen at a
    time -- while the message written for exactly this situation sat one
    branch above, unreachable.

    The two remedies differ, which is why the branches stay separate: one says
    install it, this one says it is installed and broken.
    """
    runner = FakeCli(OSError(8, "Exec format error"))
    with pytest.raises(ProviderNotReady, match="could not be executed"):
        LLMClient(cli_settings, phase="bench", cli_runner=runner).complete(request_for())


def test_a_missing_executable_still_says_install_it(cli_settings: Settings) -> None:
    """Guard the guard: the new branch must not swallow the missing case.

    `FileNotFoundError` is a subclass of `OSError`, so ordering decides which
    message a user sees. Wrong order and "install Claude Code" becomes "it
    exists but could not be executed" -- advice that is false and unactionable.
    """
    runner = FakeCli(FileNotFoundError("claude"))
    with pytest.raises(ProviderNotReady, match="not on PATH"):
        LLMClient(cli_settings, phase="bench", cli_runner=runner).complete(request_for())


def test_a_workdir_below_a_claude_md_is_refused_before_the_cli_runs(
    cli_settings: Settings, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    (project / "nested").mkdir(parents=True)
    (project / "CLAUDE.md").write_text("a build contract the model must not see")
    runner = FakeCli(cli_body())
    configured = on(cli_settings, "claude_code", workdir=str(project / "nested"))
    with pytest.raises(ProviderNotReady, match="would be loaded into every call"):
        LLMClient(configured, phase="bench", cli_runner=runner).complete(request_for())
    assert runner.calls == []


def test_a_workdir_inside_the_repository_is_refused(cli_settings: Settings) -> None:
    from cascade.config import repo_root

    inside = on(cli_settings, "claude_code", workdir=str(repo_root() / "docs"))
    with pytest.raises(ProviderNotReady, match="inside the repository"):
        LLMClient(inside, phase="bench", cli_runner=FakeCli(cli_body())).complete(request_for())


def test_the_cli_provider_cannot_carry_a_batched_phase(cli_settings: Settings) -> None:
    runner = FakeCli()
    client = LLMClient(cli_settings, phase="simulate", cli_runner=runner)
    with pytest.raises(ProviderNotReady, match="Message Batches API"):
        client.complete_batch([BatchItem(custom_id="c0", request=request_for())])
    assert runner.calls == []


def test_the_cli_environment_detaches_from_this_session_and_this_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-would-switch-billing")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
    env = claude_cli_environment()
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["MAX_THINKING_TOKENS"] == "0"
    assert "PATH" in env


class TestTheCliSchemaStrip:
    """`claude -p` refuses a keyword Pydantic emits for a tagged union.

    Measured, not guessed. The CLI exits 1 with `--json-schema is not a valid
    JSON Schema: strict mode: unknown keyword: "discriminator"`, because its
    validator treats an unrecognised keyword as an error rather than ignoring
    it -- and the §7.4 action space is a tagged union over seven types, so
    Pydantic emits one.

    This is why the compiler ran under a subscription for a whole milestone
    while the agents could not: `DRAFT_TOOL` is a plain object and carries no
    discriminator, `ACTION_TOOL` does, and nothing had tried to simulate
    through the CLI until M15.

    What is held here is that the strip is *lossless*. `discriminator` is an
    OpenAPI dispatch hint layered on `oneOf`; it says which branch to try
    first and removes no constraint, because every branch still carries its
    own `type` literal.
    """

    def test_the_refused_keyword_is_gone_at_every_depth(self) -> None:
        from cascade.llm.claude_cli import wire_schema

        nested = {
            "discriminator": {"propertyName": "type"},
            "properties": {
                "action": {
                    "discriminator": {"propertyName": "type"},
                    "oneOf": [{"discriminator": {}, "const": "COMMIT"}],
                }
            },
            "items": [{"discriminator": {}}],
        }
        assert "discriminator" not in json.dumps(wire_schema(nested))

    def test_everything_else_survives(self) -> None:
        from cascade.llm.claude_cli import wire_schema

        schema = {
            "type": "object",
            "required": ["action"],
            "additionalProperties": False,
            "properties": {"action": {"oneOf": [{"const": "COMMIT"}], "discriminator": {}}},
        }
        stripped = wire_schema(schema)
        assert stripped["type"] == "object"
        assert stripped["required"] == ["action"]
        assert stripped["additionalProperties"] is False
        assert stripped["properties"]["action"]["oneOf"] == [{"const": "COMMIT"}]

    def test_the_input_is_not_mutated(self) -> None:
        # The tool definitions are module-level singletons shared by every
        # provider. Mutating one here would strip the discriminator from the
        # schema an API provider is sent, on whichever call happened to be
        # rendered for the CLI first.
        from cascade.llm.claude_cli import wire_schema

        original = {"discriminator": {"propertyName": "type"}, "type": "object"}
        wire_schema(original)
        assert "discriminator" in original

    def test_the_real_action_tool_loses_the_keyword_and_keeps_its_actions(self) -> None:
        from cascade.llm.claude_cli import wire_schema
        from cascade.sim.prompts import ACTION_TOOL

        rendered = json.dumps(wire_schema(ACTION_TOOL["input_schema"]))
        assert "discriminator" not in rendered
        # All seven §7.4 action types still reachable, so the model sees the
        # same admissible set it would through an API provider.
        for action in ("COMMIT", "SIGNAL", "ALLY", "DEFECT", "ESCALATE", "CONCEDE", "WAIT"):
            assert action in rendered
        assert "oneOf" in rendered or "anyOf" in rendered

    def test_the_compiler_tool_is_unaffected(self) -> None:
        # The asymmetry that hid the defect: the compiler's schema never had
        # one, which is why compile worked on the subscription all along.
        from cascade.decompose.prompts import DRAFT_TOOL
        from cascade.llm.claude_cli import wire_schema

        assert wire_schema(DRAFT_TOOL["input_schema"]) == DRAFT_TOOL["input_schema"]

    def test_the_strip_reaches_the_argv(self) -> None:
        # The guard that matters operationally: it is applied where the schema
        # becomes `--json-schema`, not merely available as a helper.
        from cascade.llm.claude_cli import build_invocation
        from cascade.llm.types import LLMRequest
        from cascade.sim.prompts import ACTION_TOOL, ACTION_TOOL_NAME

        request = LLMRequest(
            model="claude-haiku-4-5-20251001",
            system=[{"type": "text", "text": "rules"}],
            messages=[{"role": "user", "content": "decide"}],
            tools=[ACTION_TOOL],
            tool_choice={"type": "tool", "name": ACTION_TOOL_NAME},
            temperature=0.7,
            max_tokens=512,
            prompt_rev="r4",
        )
        invocation = build_invocation(request, executable="claude", model="claude-haiku-4-5")
        assert "discriminator" not in " ".join(invocation.argv)

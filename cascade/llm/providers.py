"""Who serves the model, and what each server can and cannot do (ADR-0028, ADR-0053).

Four providers sit behind the one call site (invariant 5). This module is
pure -- no SDK import, no I/O, no environment read -- so it can describe them
without becoming a second door: ``cascade/llm/client.py`` remains the only
module that imports ``anthropic``, builds a client for a model-serving AWS
service, or runs the CLI, and every call it makes is cached, metered and
traced the same way.

The providers differ in ways that are load-bearing for the study, not cosmetic:

=================== ==================== ======= ================== =========== =========
provider            operated by          batches wire model id      priced by   cache
=================== ==================== ======= ================== =========== =========
anthropic           Anthropic            yes     logical            ``pricing`` shared
claude_platform_aws Anthropic, via AWS   yes     logical            its own     shared
bedrock             AWS (partner)        **no**  ``anthropic.<id>`` its own     shared
claude_cli          Claude Code CLI      **no**  logical            zero (sub.) **own**
=================== ==================== ======= ================== =========== =========

``claude_cli`` is the one provider that cannot serve the request its cache
key describes -- the CLI cannot set ``temperature`` or ``max_tokens`` -- so it
records under a namespace of its own (ADR-0031) and is never mixed with the
three API providers, which share one (ADR-0029). It is the provider in use
while the project runs on a Claude subscription; ``claude_platform_aws`` is
the deployed design, implemented and tested against the real SDK, and waits on
account provisioning (ADR-0053).

**Names.** ``claude_cli`` and ``claude_platform_aws`` are the canonical
names; ``claude_code`` and ``aws`` are the spellings M10-M16 used for the
same two providers and are accepted everywhere a name is read
(``cascade.config.normalise_provider``). The CLI provider's cache namespace
keeps its original spelling, ``claude_code``, so every recording made under
ADR-0031 still resolves: a namespace is an identity for stored bytes, not a
display name.

**Bedrock cannot carry a batched phase.** The simulate phase is inside its
$240 ceiling only at the 50% batch rate (ADR-0020); unbatched it is roughly
twice that. A provider without batches is therefore refused at the batch door
before any spend, rather than silently falling back to one call at a time.

**That refusal is about money, so it does not bind a provider that charges
none** (ADR-0052). ``claude_cli`` runs under a flat-rate subscription: its
price table is zero by construction, so a phase costs the same batched or not
and there is no discount for an unbatched run to lose. :func:`charges_per_call`
is the one place that distinction is drawn, and it reads the static
:class:`ProviderSpec` rather than the price table -- a configured table is
configuration, and a zeroed one would otherwise exempt a paid provider.

**The wire id is rendered here and nowhere else.** Requests carry the logical
model everywhere else -- in the cache key (ADR-0029), in the price lookup, in
the event log -- so switching provider changes where a call is sent and how it
is billed, and never which recording it resolves to.

**The interface.** :class:`ModelProvider` is the shape every provider class
in ``client.py`` has: one ``complete`` that returns the provider's own
Messages body with the request id it answered under, one ``batches`` that
either returns the SDK's batch resource or refuses, and a static
:class:`ProviderSpec`. It is a Protocol defined here, where nothing can reach
a network, so the shell can ask the same questions of every provider without
importing the module that does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from cascade.config import PROVIDER_ALIASES, LLMProvider, Settings, normalise_provider
from cascade.llm.types import LLMRequest, ProviderResponse

__all__ = [
    "PROVIDERS",
    "ModelProvider",
    "ProviderSpec",
    "charges_per_call",
    "client_kwargs",
    "endpoint",
    "readiness_problems",
    "render_model",
    "spec_for",
]

_BEDROCK_MODEL_PREFIX = "anthropic."

# The SDK's own endpoint templates. Reproduced rather than delegated because
# the SDK consults ANTHROPIC_AWS_BASE_URL / ANTHROPIC_BEDROCK_MANTLE_BASE_URL
# from the ambient environment *before* falling back to the region, so leaving
# base_url unset would let a stray variable redirect the study's traffic.
# tests/unit/test_llm_providers.py asserts these agree with what the installed
# SDK derives, so the copy cannot drift silently.
_ENDPOINT_TEMPLATES: dict[str, str] = {
    "claude_platform_aws": "https://aws-external-anthropic.{region}.api.aws",
    "bedrock": "https://bedrock-mantle.{region}.api.aws/anthropic",
}


@dataclass(frozen=True)
class ProviderSpec:
    """What one provider is, independent of how it is configured."""

    name: LLMProvider
    operated_by: str
    supports_batches: bool
    # Name of the client class in the `anthropic` package. A string, because
    # this module must not import the SDK (invariant 5). None for the CLI.
    client_class: str | None
    pricing_source: str
    billing: Literal["per_token", "subscription"] = "per_token"
    # None means the shared API namespace (ADR-0029). A provider that cannot
    # honour every field of the cache key domain must set one (ADR-0031).
    cache_namespace: str | None = None
    # Spellings an older configuration may carry for this provider (ADR-0053).
    aliases: tuple[str, ...] = ()
    # Where the provider stands in this project, in one line, for `doctor`
    # and the README: the brief's own vocabulary.
    status: str = ""


PROVIDERS: dict[LLMProvider, ProviderSpec] = {
    "anthropic": ProviderSpec(
        name="anthropic",
        operated_by="Anthropic",
        supports_batches=True,
        client_class="Anthropic",
        pricing_source="https://www.anthropic.com/pricing",
        status="the pinned study configuration; needs a pay-as-you-go key",
    ),
    "claude_platform_aws": ProviderSpec(
        name="claude_platform_aws",
        operated_by="Anthropic, through AWS (Claude Platform on AWS)",
        supports_batches=True,
        client_class="AnthropicAWS",
        pricing_source="the Claude Platform on AWS listing in AWS Marketplace",
        aliases=("aws",),
        status="implemented and mock-tested; not enabled -- account provisioning pending",
    ),
    "bedrock": ProviderSpec(
        name="bedrock",
        operated_by="AWS (Amazon Bedrock)",
        supports_batches=False,
        client_class="AnthropicBedrockMantle",
        pricing_source="https://aws.amazon.com/bedrock/pricing/",
        status="implemented and mock-tested; cannot carry a batched phase",
    ),
    "claude_cli": ProviderSpec(
        name="claude_cli",
        operated_by="the Claude Code CLI (headless, under a Claude subscription)",
        supports_batches=False,
        client_class=None,
        pricing_source="a Claude subscription, which bills no tokens",
        billing="subscription",
        # Deliberately the pre-ADR-0053 spelling: recordings made under ADR-0031
        # are filed under this namespace, and a renamed namespace would turn
        # every one of them into a replay miss.
        cache_namespace="claude_code",
        aliases=("claude_code",),
        status="ACTIVE -- the current provider; not the pinned configuration (ADR-0031)",
    ),
}


def spec_for(provider: str) -> ProviderSpec:
    """Return the static description of ``provider``, accepting its aliases."""
    return PROVIDERS[normalise_provider(provider)]


def charges_per_call(provider: str) -> bool:
    """Whether ADR-0020's ceiling argument binds ``provider``. Pure.

    Preserves the invariant that a phase priced at the 50% batch rate is never
    run unbatched at list price. ADR-0020 made batching functional because
    simulate is $126 batched and $252 not against a $240 ceiling -- an argument
    about *money*, which is why it does not bind a provider that charges none:
    a subscription's price table is zero by construction
    (``Settings.pricing_table``), so the same phase costs the same either way.

    Three properties make this hard to get around, and each has a test:

    - It reads :attr:`ProviderSpec.billing`, a frozen module constant. Reading
      the price table instead would let ``providers.bedrock.pricing`` zeroed in
      a YAML file exempt a paid provider from the ceiling that guards the
      study's cost claim.
    - It keys off billing, not off the provider's name, so a fifth provider
      added later is bound unless someone writes ``subscription`` next to it.
    - An unrecognised name is charged. A typo must not buy an exemption.

    Read live from :data:`PROVIDERS` rather than from a table built at import,
    so the registry and this answer cannot disagree. Aliases resolve to their
    canonical name first, so ``claude_code`` is as exempt as ``claude_cli``.
    """
    canonical = PROVIDER_ALIASES.get(provider, provider)
    billing: dict[str, str] = {name: PROVIDERS[name].billing for name in sorted(PROVIDERS)}
    return billing.get(canonical, "per_token") != "subscription"


def render_model(settings: Settings, logical: str) -> str:
    """The model id the active provider expects on the wire. Pure.

    Preserves ADR-0029's invariant that only the wire payload ever carries a
    provider-specific id. Bedrock takes an ``anthropic.``-prefixed id; an entry
    in ``providers.bedrock.model_ids`` overrides the derived form, so a
    deployment that publishes a different id is a configuration change rather
    than a code change.
    """
    if settings.llm.provider != "bedrock":
        return logical
    override = settings.providers.bedrock.model_ids.get(logical)
    if override is not None:
        return override
    return f"{_BEDROCK_MODEL_PREFIX}{logical}"


def endpoint(settings: Settings) -> str | None:
    """The base URL the active provider's requests go to. Pure.

    ``None`` for the first-party provider, whose SDK default is not
    environment-derived in a way this study configures, and for the CLI,
    which has no URL. For the AWS providers it is the configured ``base_url``
    if one is set, else the documented regional endpoint -- and ``None`` when
    there is no region to derive it from, which :func:`readiness_problems`
    reports.
    """
    provider = settings.llm.provider
    if provider in ("anthropic", "claude_cli"):
        return None
    section = (
        settings.providers.claude_platform_aws
        if provider == "claude_platform_aws"
        else settings.providers.bedrock
    )
    if section.base_url:
        return section.base_url
    if not section.region:
        return None
    return _ENDPOINT_TEMPLATES[provider].format(region=section.region)


def readiness_problems(settings: Settings, *, models: tuple[str, ...]) -> list[str]:
    """Everything that stops the active provider serving ``models``. Pure.

    Preserves the invariant that no money is spent through a provider that
    could not be routed explicitly or priced exactly. Returns every problem at
    once, sorted, so an operator fixes the configuration in one pass instead of
    discovering the next gap only after fixing the last.
    """
    provider = settings.llm.provider
    problems: list[str] = []

    if provider == "anthropic":
        key = settings.anthropic_api_key
        # An empty variable is not an absent one: `KEY=` in a .env parses to
        # SecretStr(""), which the SDK rejects with an error naming neither the
        # variable nor this project.
        if key is None or not key.get_secret_value().strip():
            problems.append("CASCADE_ANTHROPIC_API_KEY is not set")
    elif provider == "claude_platform_aws":
        platform = settings.providers.claude_platform_aws
        if not platform.region:
            problems.append("providers.claude_platform_aws.region is not set")
        if not platform.workspace_id:
            problems.append(
                "providers.claude_platform_aws.workspace_id is not set -- set "
                "ANTHROPIC_AWS_WORKSPACE_ID (or CASCADE_PROVIDERS__CLAUDE_PLATFORM_AWS__WORKSPACE_ID)"
            )
    elif provider == "bedrock":
        if not settings.providers.bedrock.region:
            problems.append("providers.bedrock.region is not set")
    else:
        if not settings.providers.claude_cli.executable.strip():
            problems.append("providers.claude_cli.executable is not set")

    if spec_for(provider).billing == "subscription":
        # Nothing to price: a subscription bills no tokens.
        return sorted(problems)
    table = settings.pricing_table()
    section = "pricing" if provider == "anthropic" else f"providers.{provider}.pricing"
    for model in sorted(set(models)):
        if model not in table:
            problems.append(
                f"{section} has no entry for {model!r} -- read it from "
                f"{spec_for(provider).pricing_source}"
            )
    return sorted(problems)


def client_kwargs(settings: Settings) -> dict[str, Any]:
    """Constructor arguments for the active provider's SDK client. Pure.

    Preserves the invariant that routing is explicit. Every routing value is
    passed even when it is the one the SDK would have found on its own,
    because the SDK's fallback reads ``AWS_REGION`` and
    ``ANTHROPIC_AWS_WORKSPACE_ID`` from the ambient environment -- the
    workspace id is admitted from that variable only by way of ``Settings``,
    where `doctor` shows it (ADR-0053), and the region never is.

    Identity is deliberately *not* passed for the AWS providers: credentials
    come from the standard AWS chain (a task or instance role in the cloud, a
    profile locally), which is the point of using IAM rather than a key.
    """
    common: dict[str, Any] = {
        "max_retries": settings.llm.max_retries,
        "timeout": settings.llm.timeout_s,
    }
    provider = settings.llm.provider
    if provider == "claude_cli":
        raise ValueError("claude_cli is a CLI, not an SDK client; it takes no constructor")
    if provider == "anthropic":
        key = settings.anthropic_api_key
        return {**common, "api_key": key.get_secret_value() if key is not None else None}
    if provider == "claude_platform_aws":
        platform = settings.providers.claude_platform_aws
        routed: dict[str, Any] = {
            "aws_region": platform.region,
            "workspace_id": platform.workspace_id,
            "base_url": endpoint(settings),
        }
        if platform.profile:
            routed["aws_profile"] = platform.profile
        return {**common, **routed}
    bedrock = settings.providers.bedrock
    routed = {"aws_region": bedrock.region, "base_url": endpoint(settings)}
    if bedrock.profile:
        routed["aws_profile"] = bedrock.profile
    return {**common, **routed}


@runtime_checkable
class ModelProvider(Protocol):
    """One way of reaching a Claude model (ADR-0053).

    Every provider class in ``cascade/llm/client.py`` has this shape, and
    :class:`~cascade.llm.client.LLMClient` talks to whichever one the
    configuration names through it alone -- so caching, metering, tracing and
    the record/replay contract are written once, above this seam, and a fifth
    provider is a class here and a row in :data:`PROVIDERS`, nothing else.

    Narrow on purpose. ``complete`` takes the request the cache key was
    computed over and returns the provider's own Messages body; it is given
    nothing about the cache, the meter or the tracer, which is what keeps a
    provider from being able to bypass any of them.
    """

    @property
    def spec(self) -> ProviderSpec:
        """The static description of this provider."""
        ...  # pragma: no cover -- protocol

    @property
    def constructed_sdk_client(self) -> bool:
        """Whether an SDK client has been built. Replay must never build one."""
        ...  # pragma: no cover -- protocol

    def sdk_client(self) -> Any:
        """The SDK client for an SDK-served provider; :class:`LLMError` for the CLI."""
        ...  # pragma: no cover -- protocol

    def complete(self, request: LLMRequest) -> ProviderResponse:
        """Serve one request and return the raw Messages body with its request id."""
        ...  # pragma: no cover -- protocol

    def batches(self) -> Any:
        """The SDK's Message Batches resource, or :class:`ProviderNotReady`."""
        ...  # pragma: no cover -- protocol

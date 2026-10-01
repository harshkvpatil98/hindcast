"""The single LLM call site (invariant 5, spec §8.3).

This is the only module in ``cascade/`` permitted to import the Anthropic SDK.
``tests/unit/test_invariants.py`` greps the tree and fails CI on a second
importer, because determinism, caching, cost metering and tracing all assume
there is exactly one door.

Three modes, and the difference between them is the whole point:

``record``
    A miss calls the API and persists ``(key, response, usage, latency)``.
``replay``
    A miss raises :class:`CacheMiss`. It never falls back to the network, and
    it never even constructs an SDK client -- so a replay run works with the
    API key unset, which is how the M0 acceptance test proves it.
``live``
    Bypasses the cache entirely. Used only by the M3 latency benchmark, where
    the thing being measured is the provider's own round trip.

Four providers can sit behind this door (ADR-0028, ADR-0031, ADR-0053), each
a class in this module implementing :class:`~cascade.llm.providers.ModelProvider`:

* :class:`AnthropicApiProvider` -- the Anthropic API, with a key;
* :class:`ClaudePlatformAwsProvider` -- Claude Platform on AWS through the
  SDK's ``AnthropicAWS`` client (IAM, batches); implemented and tested
  against the real SDK, enabled once the account is provisioned;
* :class:`BedrockProvider` -- Amazon Bedrock through ``AnthropicBedrockMantle``;
* :class:`ClaudeCliProvider` -- the Claude Code CLI run headless under the
  operator's own subscription; the provider in use today.

The three SDK client classes live in the one ``anthropic`` package, so this
module is still the only importer of it, and it is also the only place the CLI
is run and the only place a ``boto3`` client for a model-serving AWS service
is built -- so every call, whichever provider serves it, is cached, metered
and traced the same way. :class:`LLMClient` owns that discipline and talks to
the provider through the interface alone. What differs between the providers
is described in :mod:`cascade.llm.providers`, which is pure.
"""

from __future__ import annotations

import math
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cascade.canonical import canonical_json
from cascade.config import LLMProvider, Settings, claude_cli_environment, repo_root
from cascade.llm.cache import CallCache, cache_domain, cache_key
from cascade.llm.claude_cli import build_invocation, parse_cli_output, to_messages_payload
from cascade.llm.meter import CostMeter
from cascade.llm.providers import (
    ModelProvider,
    ProviderSpec,
    charges_per_call,
    client_kwargs,
    endpoint,
    readiness_problems,
    render_model,
    spec_for,
)
from cascade.llm.tracing import Tracer, null_tracer
from cascade.llm.types import (
    SOURCE,
    BatchFailed,
    BatchItem,
    CachedCall,
    CacheMiss,
    LLMError,
    LLMRequest,
    LLMResult,
    PromptTooShortToCache,
    ProviderNotReady,
    ProviderResponse,
    ScreenResult,
    Usage,
)
from cascade.retrieval.rerank import LexicalReranker, RerankError, rerank_cache_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

__all__ = [
    "BATCH_MAX_REQUESTS",
    "RERANK_BILLING_KIND",
    "AnthropicApiProvider",
    "AwsAccessReport",
    "AwsCheck",
    "BedrockGuardrail",
    "BedrockProvider",
    "BedrockReranker",
    "ClaudeCliProvider",
    "ClaudePlatformAwsProvider",
    "CliCompleted",
    "CliRunner",
    "LLMClient",
    "RecordedReranker",
    "assert_cacheable_prefix",
    "build_provider",
    "describe_aws_access",
    "estimate_tokens",
]

# Provider limits on one batch submission: 100,000 requests or 256 MB. A wave
# of the M6 fan-out is 36,000 runs x ~4.86 active actors, so a step can exceed
# the request limit and has to be chunked; the byte limit is checked as the
# payload is assembled rather than assumed.
BATCH_MAX_REQUESTS = 100_000
BATCH_MAX_BYTES = 256 * 1024 * 1024

# Claude tokenises English prose at roughly 3.6 characters per token. The
# estimator is only used to gate the cacheable prefix (ADR-0001); it is
# deliberately *not* used for billing, which always uses the provider's own
# reported counts.
_CHARS_PER_TOKEN = 3.6


# API statuses worth retrying through the CLI: the provider's own transient
# failures. 429 is deliberately absent -- under a subscription it usually means
# the plan's usage limit, which retrying in a loop only extends.
_CLI_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504, 529})

# The CLI reports its own exhausted structured-output retries as an error with
# no HTTP status, so it matches neither the 429 guard nor the statuses above
# and would otherwise be fatal. Measured in the M15 fan-out: three of
# thirty-six scenarios died about fifteen minutes in, each losing all ten of
# its replicates, to one identical cause -- `claude -p` exhausting its internal
# attempts to emit a parseable tool call, arriving as
# `subtype='error_max_structured_output_retries' status=None`, falling through
# to `to_messages_payload` and raising.
#
# It is transient in exactly the sense a 529 is: the model failed to emit a
# parseable tool call that time, and a fresh call may succeed. Retrying here
# does not fabricate a decision -- a failed parse is not an action, so
# ADR-0018's coercion to WAIT does not apply. That coercion belongs to
# `_tool_input()` returning None, which is a different fact: there the CLI
# answered and the answer carried no usable action, where this subtype means
# no answer came back at all.
#
# A whitelist rather than a blanket retry on `is_error`, so an unrecognised
# failure still fails loudly instead of looping in silence.
_CLI_TRANSIENT_SUBTYPES = frozenset({"error_max_structured_output_retries"})


# The label per-query rerank spend is booked under in the cost meter. One
# constant, because the wrapper books the hits and the reranker books the
# misses (see `RecordedReranker`), and two spellings would split one number
# into two.
RERANK_BILLING_KIND = "rerank"

# Amazon Bedrock's Rerank API, read from the service model that ships with the
# installed botocore (`bedrock-agent-runtime`, 2023-07-26) and from the AWS API
# reference, never from memory. Every constant below is a published
# constraint, and each one is here because exceeding it silently is worse than
# failing on it:
#
#   sources       1..1000 items        -- a pool of 1,001 cannot be scored
#   queries       exactly 1 item       -- one query per call, hence one billed unit
#   textDocument  1..32,000 characters -- an empty or oversized body is refused by AWS
#   index         0..1000, and is "the original index of the document from the
#                 input sources array" -- which is why the response has to be
#                 mapped back rather than read in the order it arrives
_RERANK_SERVICE = "bedrock-agent-runtime"
_RERANK_MAX_SOURCES = 1000
_RERANK_MAX_DOCUMENT_CHARS = 32_000
# How the service bills: "Reranking is priced per query. A query is a single
# call to the reranker model that can contain up to 100 document chunks"
# (Amazon Bedrock user guide, "Pricing and search units", read 2026-09-30).
# A pool wider than that is therefore more than one search unit, and the meter
# books it as such rather than as one.
_RERANK_DOCUMENTS_PER_QUERY = 100

# Headers a provider files a call under, in the order they are looked for:
# the Messages API's own, AWS's, and the generic one some gateways set.
_REQUEST_ID_HEADERS = ("request-id", "x-amzn-requestid", "x-request-id")


@dataclass(frozen=True)
class CliCompleted:
    """What one ``claude -p`` process returned."""

    returncode: int
    stdout: str
    stderr: str


# (argv, stdin, working directory, timeout seconds) -> result. Injectable, as
# `http_client` is for the SDK providers, so no test ever runs the real CLI or
# spends a subscription's allowance.
CliRunner = Callable[[Sequence[str], str, Path, float], CliCompleted]


def _run_cli(argv: Sequence[str], stdin: str, workdir: Path, timeout_s: float) -> CliCompleted:
    """Run the Claude Code CLI once, with the isolated environment (ADR-0031)."""
    completed = subprocess.run(  # noqa: S603 -- argv from build_invocation, no shell
        list(argv),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=workdir,
        env=claude_cli_environment(),
        check=False,
    )
    return CliCompleted(completed.returncode, completed.stdout, completed.stderr)


def estimate_tokens(text: str) -> int:
    """Approximate the token count of ``text`` without a network round trip.

    Preserves the invariant that the prefix-length gate is checkable offline
    and in CI. This is an estimate and is never used for cost: every dollar in
    the ledger comes from the provider's reported ``usage``.
    """
    return int(len(text) / _CHARS_PER_TOKEN)


def assert_cacheable_prefix(
    prefix: str,
    settings: Settings,
    *,
    exact_tokens: int | None = None,
) -> int:
    """Fail loudly if a cacheable prefix sits below the provider's cache floor.

    Preserves the cost model in spec §12.1, which assumes the 1,900-token
    persona prefix is billed at the 10% cached-read rate. Anthropic does not
    error on a short prefix -- it silently declines to cache, and the only
    visible symptom is a ledger that is ~4.8x over. See ADR-0001.

    Returns the token count used for the decision.
    """
    tokens = exact_tokens if exact_tokens is not None else estimate_tokens(prefix)
    if not settings.prompt_cache.enabled:
        return tokens
    floor = settings.prompt_cache.min_prefix_tokens
    if tokens < floor and settings.prompt_cache.enforce_min_prefix:
        raise PromptTooShortToCache(measured_tokens=tokens, minimum_tokens=floor)
    return tokens


class LLMClient:
    """Mediates every model call: cache, meter, tracer, then maybe the network."""

    def __init__(
        self,
        settings: Settings,
        *,
        phase: str,
        meter: CostMeter | None = None,
        cache: CallCache | None = None,
        tracer: Tracer | None = None,
        http_client: httpx.Client | None = None,
        cli_runner: CliRunner | None = None,
    ) -> None:
        self._settings = settings
        self._phase = phase
        self._mode = settings.llm.mode
        self.cache = cache if cache is not None else CallCache(settings.cache_path())
        self.meter = meter if meter is not None else CostMeter(settings, phase)
        self._tracer = tracer if tracer is not None else null_tracer()
        self._provider: LLMProvider = settings.llm.provider
        # The one object that reaches a provider, chosen by name (ADR-0053).
        # Everything above it -- cache, meter, tracer, replay -- is provider-
        # agnostic. `sleep` is late-bound so a test may patch `_sleep` on the
        # class before or after construction.
        self._backend: ModelProvider = build_provider(
            settings,
            http_client=http_client,
            cli_runner=cli_runner,
            sleep=lambda seconds: self._sleep(seconds),
        )
        # None for the three API providers, which share one namespace
        # (ADR-0029); the CLI gets its own (ADR-0031).
        self._namespace = self._backend.spec.cache_namespace

        # Fail before the first dollar, not at the first miss. A provider that
        # cannot be routed explicitly or priced exactly would either send the
        # call somewhere nobody chose or book it at a rate nobody checked, and
        # both surface only at reconciliation. Replay never reaches a provider,
        # so it needs none of this -- which is what lets `make demo` run with
        # no credential of any kind.
        if self._mode != "replay":
            problems = readiness_problems(
                settings, models=(settings.models.agent, settings.models.compiler)
            )
            if problems:
                raise ProviderNotReady(self._provider, problems)

    # -- properties ---------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def backend(self) -> ModelProvider:
        """The provider serving this client, behind its interface."""
        return self._backend

    @property
    def constructed_sdk_client(self) -> bool:
        """Whether an SDK client has ever been built in this process.

        The M0 acceptance criterion is that replay makes zero network calls.
        A transport that raises proves no call was *sent*; this proves none
        could have been, because no client exists to send one.
        """
        return self._backend.constructed_sdk_client

    # -- the one door -------------------------------------------------------

    def complete(
        self,
        request: LLMRequest,
        *,
        batch: bool = False,
        trace_name: str = "llm.complete",
    ) -> LLMResult:
        """Execute ``request`` under the configured mode and book its cost.

        Preserves invariant 5 (one call site) and the record/replay contract:
        in ``replay`` this function is total with respect to the recorded
        corpus -- it either returns a recorded response or raises, and there
        is no third outcome that reaches the network.
        """
        if batch:
            # One request is not a batch. The Batches API is asynchronous with
            # an SLA measured in hours, so a single-request submission would
            # trade minutes of latency for half a cent; `complete_batch` is the
            # entry point that makes the discount worth having.
            raise LLMError(
                "complete(batch=True) submits one request and waits hours for it. "
                "Use complete_batch() with a whole wave of requests instead."
            )

        key = cache_key(request, namespace=self._namespace)

        if self._mode == "live":
            return self._call_api(request, key=key, batch=batch, trace_name=trace_name, store=False)

        recorded = self.cache.get(key)
        if recorded is not None:
            return self._from_recording(recorded, trace_name=trace_name)

        if self._mode == "replay":
            raise CacheMiss(
                f"no recorded response for key {key} (model={request.model!r}, "
                f"prompt_rev={request.prompt_rev!r}) in {self.cache.root}. "
                "Replay never falls back to the network -- re-record with "
                "CASCADE_LLM__MODE=record if this call is new."
            )

        return self._call_api(request, key=key, batch=batch, trace_name=trace_name, store=True)

    # -- the batch door (spec §12.2) ----------------------------------------

    def complete_batch(
        self,
        items: Sequence[BatchItem],
        *,
        trace_name: str = "llm.batch",
        poll_interval_s: float = 5.0,
        timeout_s: float = 86_400.0,
    ) -> dict[str, LLMResult]:
        """Serve a whole wave of requests, batching whatever the cache misses.

        Returns ``custom_id -> LLMResult`` for every item. Cache hits are
        served locally and never reach the network, which is what makes the
        88% hit rate the M6 criterion measures a *saving* rather than a
        statistic -- only the misses are submitted, and they are submitted at
        the 50% batch rate the §12.1 cost model assumes.

        Identical requests inside one wave are submitted **once**. Two
        replicates whose trajectories have not diverged produce byte-identical
        requests, and paying twice for them would spend the study's budget on
        the very duplication the cache exists to exploit.

        In ``replay`` a miss raises :class:`CacheMiss`, exactly as
        :meth:`complete` does: replay never reaches the network by any door.
        """
        if self._mode == "live":
            # `complete` bypasses the cache in live mode, so a batched answer
            # would be re-requested one call at a time at DECIDE and the wave
            # would be paid for twice. Live is for the latency benchmark, where
            # the provider's own round trip is the thing being measured.
            raise LLMError(
                "complete_batch is not available in live mode: live bypasses the cache, "
                "so every batched answer would be requested again at decide time. "
                "Use record (to pay once and keep the responses) or replay."
            )

        results: dict[str, LLMResult] = {}
        pending: dict[str, list[str]] = {}
        by_key: dict[str, LLMRequest] = {}

        for item in sorted(items, key=lambda entry: entry.custom_id):
            key = cache_key(item.request, namespace=self._namespace)
            recorded = self.cache.get(key)
            if recorded is not None:
                results[item.custom_id] = self._from_recording(recorded, trace_name=trace_name)
                continue
            if self._mode == "replay":
                raise CacheMiss(
                    f"no recorded response for key {key} (custom_id={item.custom_id!r}, "
                    f"model={item.request.model!r}, prompt_rev={item.request.prompt_rev!r}) "
                    f"in {self.cache.root}. Replay never falls back to the network -- "
                    "re-record with CASCADE_LLM__MODE=record if this call is new."
                )
            pending.setdefault(key, []).append(item.custom_id)
            by_key[key] = item.request

        if not pending:
            return results

        # Checked only once there is something to submit: a wave served
        # entirely from recordings costs nothing on any provider, so replaying
        # a batched study through Bedrock is fine. Submitting one is not --
        # there is no batch endpoint, and the per-call fallback would run the
        # phase at roughly twice the rate its ceiling was set against.
        if not self._backend.spec.supports_batches:
            # Refused either way -- there is no batch endpoint to reach, and
            # looping `complete` here would make one function mean two things
            # for every caller, including the ones that need the discount. The
            # remedy differs, so the message names the right one: a provider
            # that charges per call has to record the phase somewhere the
            # ceiling was set against, while one that charges nothing has no
            # ceiling to breach and needs a caller that prepares serially
            # (ADR-0052). Both answers are wrong for the other provider.
            remedy = (
                "Record this phase through `anthropic` or `claude_platform_aws` -- the "
                "recordings then replay through any provider (ADR-0029)"
                if charges_per_call(self._provider)
                else "This provider bills a subscription rather than tokens, so the "
                "ceiling argument does not apply to it: resolve the wave through a "
                "decider that prepares serially (`LLMAgents.prepare`, ADR-0052) "
                "rather than through this door"
            )
            raise ProviderNotReady(
                self._provider,
                [
                    f"{len(pending)} uncached request(s) need the Message Batches API, which "
                    f"{self._backend.spec.operated_by} does not provide; the batched "
                    f"phase's ceiling assumes the batch rate (ADR-0020). {remedy}"
                ],
            )

        for chunk in _chunked(sorted(pending), BATCH_MAX_REQUESTS):
            served = self._run_batch(
                {key: by_key[key] for key in chunk},
                trace_name=trace_name,
                poll_interval_s=poll_interval_s,
                timeout_s=timeout_s,
            )
            for key, result in sorted(served.items()):
                for custom_id in pending[key]:
                    results[custom_id] = result

        missing = sorted(set(_flatten(pending)) - set(results))
        if missing:
            raise BatchFailed({custom_id: "no result returned" for custom_id in missing})
        return results

    def _run_batch(
        self,
        requests: Mapping[str, LLMRequest],
        *,
        trace_name: str,
        poll_interval_s: float,
        timeout_s: float,
    ) -> dict[str, LLMResult]:
        """Submit one batch keyed by cache key, wait for it, and price the results."""
        batches = self._backend.batches()
        payload = [
            {
                "custom_id": _batch_id(key),
                "params": _messages_params(
                    requests[key], model=render_model(self._settings, requests[key].model)
                ),
            }
            for key in sorted(requests)
        ]
        size = len(canonical_json(payload).encode("utf-8"))
        if size > BATCH_MAX_BYTES:
            raise LLMError(
                f"batch payload is {size} bytes, over the provider's "
                f"{BATCH_MAX_BYTES}-byte limit; submit fewer requests per wave"
            )

        started = time.perf_counter()
        batch = batches.create(requests=payload)
        batch_id = str(getattr(batch, "id", ""))
        status = str(getattr(batch, "processing_status", ""))
        while status != "ended":
            if time.perf_counter() - started > timeout_s:
                raise LLMError(
                    f"batch {batch_id} still {status!r} after {timeout_s:.0f}s; "
                    "the provider's own SLA is 24h -- raise the timeout or cancel it"
                )
            self._sleep(poll_interval_s)
            batch = batches.retrieve(batch_id)
            status = str(getattr(batch, "processing_status", ""))

        out: dict[str, LLMResult] = {}
        failures: dict[str, str] = {}
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        by_batch_id = {_batch_id(key): key for key in sorted(requests)}

        for entry in batches.results(batch_id):
            key = by_batch_id.get(str(getattr(entry, "custom_id", "")))
            if key is None:
                continue
            outcome = getattr(entry, "result", None)
            kind = str(getattr(outcome, "type", "")) if outcome is not None else ""
            if kind != "succeeded":
                failures[key] = kind or "unknown"
                continue
            raw = _message_payload(outcome)
            usage = _usage_from_payload(raw)
            result = _result_from_payload(
                raw, usage=usage, latency_ms=elapsed_ms, served_from_cache=False
            )
            # Persist before metering. The meter raises when the phase ceiling
            # breaks, and a response that has already been paid for must not be
            # discarded by the abort -- the resumed phase would buy it again.
            if self._mode == "record":
                self.cache.put(
                    CachedCall(
                        key=key,
                        request_digest=cache_domain(requests[key], namespace=self._namespace),
                        raw_response=raw,
                        usage=usage,
                        latency_ms=elapsed_ms,
                        recorded_at=datetime.now(UTC).isoformat(),
                        provider_metadata={
                            "provider": self._provider,
                            "batch_id": batch_id,
                            "wire_model": payload_model(raw),
                        },
                    )
                )
            # Priced by the logical model the request named, not the string the
            # response echoes: providers disagree on that string, and the price
            # of a call is a property of what was asked for.
            cost = self.meter.record(model=requests[key].model, usage=usage, batch=True)
            self._tracer.generation(
                name=trace_name,
                model=result.model,
                usage=usage,
                cost_usd=cost,
                cached=False,
                latency_ms=elapsed_ms,
                provider=self._provider,
                request_id=batch_id or None,
            )
            out[key] = result

        if failures:
            raise BatchFailed(failures)
        return out

    def _sleep(self, seconds: float) -> None:
        """Wait between polls. Isolated so a test can drive the loop instantly."""
        time.sleep(seconds)

    # -- internals ----------------------------------------------------------

    def _from_recording(self, recorded: CachedCall, *, trace_name: str) -> LLMResult:
        """Rebuild a result from disk and book it as a zero-cost generation.

        Cache hits are traced rather than skipped (spec §12.2): hit rate and
        spend have to appear on the same dashboard, or the biggest cost lever
        in the study is invisible.
        """
        metadata = recorded.provider_metadata or {}
        result = _result_from_payload(
            recorded.raw_response,
            usage=recorded.usage,
            latency_ms=recorded.latency_ms,
            served_from_cache=True,
            request_id=_optional_str(metadata.get("request_id")),
        )
        self.meter.record_cache_hit(model=result.model, usage=result.usage)
        self._tracer.generation(
            name=trace_name,
            model=result.model,
            usage=result.usage,
            cost_usd=None,
            cached=True,
            latency_ms=recorded.latency_ms,
            provider=self._provider,
            request_id=result.request_id,
        )
        return result

    def _client(self) -> Any:
        """The provider's SDK client, constructed lazily, exactly once.

        Lazy so that ``replay`` never builds one -- see
        :attr:`constructed_sdk_client`. Kept as a method on the client for the
        callers that need the SDK object itself (the latency bench).
        """
        return self._backend.sdk_client()

    def _call_api(
        self,
        request: LLMRequest,
        *,
        key: str,
        batch: bool,
        trace_name: str,
        store: bool,
    ) -> LLMResult:
        """Send one request, price it, and persist it when recording."""
        started = time.perf_counter()
        response = self._backend.complete(request)
        latency_ms = (time.perf_counter() - started) * 1000.0
        raw = response.body

        usage = _usage_from_payload(raw)
        result = _result_from_payload(
            raw,
            usage=usage,
            latency_ms=latency_ms,
            served_from_cache=False,
            request_id=response.request_id,
        )

        cost = self.meter.record(model=request.model, usage=usage, batch=batch)
        self._tracer.generation(
            name=trace_name,
            model=result.model,
            usage=usage,
            cost_usd=cost,
            cached=False,
            latency_ms=latency_ms,
            provider=self._provider,
            request_id=response.request_id,
        )

        if store:
            self.cache.put(
                CachedCall(
                    key=key,
                    request_digest=cache_domain(request, namespace=self._namespace),
                    raw_response=raw,
                    usage=usage,
                    latency_ms=latency_ms,
                    recorded_at=datetime.now(UTC).isoformat(),
                    provider_metadata={
                        "provider": self._provider,
                        "request_id": response.request_id,
                        **{k: response.metadata[k] for k in sorted(response.metadata)},
                    },
                )
            )
        return result


def _status_code(value: Any) -> int | None:
    """The CLI reports an API status as an int, a numeric string, or null."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _batch_id(key: str) -> str:
    """The provider-facing id for a request, derived from its cache key.

    Deriving rather than counting means the id is stable across resubmissions
    and identical across processes, so a resumed wave that re-submits the same
    misses lines its results up with the same requests.
    """
    return f"k{key[:56]}"


def _optional_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def payload_model(raw: Mapping[str, Any]) -> str | None:
    """The model string a response body echoes, if any -- metadata, never a price key."""
    return _optional_str(raw.get("model"))


def _request_id_from_headers(headers: Any) -> str | None:
    """The provider's request id from a response's headers, or None.

    Looked up by the names each provider documents, case-insensitively, so
    the same code reads the Messages API's `request-id` and AWS's
    `x-amzn-requestid`. Absence is None, never an empty string: a recording
    must be able to say "the provider sent no id" distinctly from "an id that
    happens to be blank".
    """
    if headers is None:
        return None
    for name in _REQUEST_ID_HEADERS:
        try:
            value = headers.get(name)
        except AttributeError:
            return None
        if value:
            return str(value)
    return None


def _messages_params(request: LLMRequest, *, model: str) -> dict[str, Any]:
    """Project a request onto the Messages API's parameters.

    One projection for the single-call and the batch door, so the two cannot
    drift: a field sent on one path and not the other would make a recording
    made through batches answer a different request from the one `complete`
    would send. ``model`` is the provider's wire id, rendered by the caller;
    the request itself only ever carries the logical model (ADR-0029).
    """
    params: dict[str, Any] = {
        "model": model,
        "messages": request.messages,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    if request.system is not None:
        params["system"] = request.system
    if request.tools:
        params["tools"] = request.tools
    if request.tool_choice is not None:
        params["tool_choice"] = request.tool_choice
    return params


def _message_payload(outcome: Any) -> dict[str, Any]:
    """Extract the Messages body from a succeeded batch result entry."""
    message = getattr(outcome, "message", None)
    if message is None:
        raise LLMError("batch result reported success with no message body")
    if isinstance(message, dict):
        return dict(message)
    dumped = message.model_dump(mode="json")
    return dict(dumped)


def _chunked(keys: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(keys), size):
        yield list(keys[start : start + size])


def _flatten(pending: Mapping[str, list[str]]) -> list[str]:
    return [custom_id for key in sorted(pending) for custom_id in pending[key]]


def _usage_from_payload(raw: dict[str, Any]) -> Usage:
    """Extract token counts from a Messages response body.

    Missing counts are an error, not a zero: a silently zero-token call would
    under-report spend, and the ledger reconciliation at M8 (±2%) is the only
    thing that would notice.
    """
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        raise LLMError(f"response carries no usage block; cannot price it: {raw.get('id')!r}")
    return Usage(
        input_tokens=int(usage["input_tokens"]),
        output_tokens=int(usage["output_tokens"]),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
    )


def _result_from_payload(
    raw: dict[str, Any],
    *,
    usage: Usage,
    latency_ms: float,
    served_from_cache: bool,
    request_id: str | None = None,
) -> LLMResult:
    """Project a stored response body onto the boundary type.

    Reads the persisted body directly rather than through the SDK so that
    replay has no dependency on the SDK being installed or configured.
    """
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in raw.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            tool_calls.append(block)
    return LLMResult(
        text="".join(text_parts),
        model=str(raw.get("model", "")),
        stop_reason=raw.get("stop_reason"),
        usage=usage,
        latency_ms=latency_ms,
        served_from_cache=served_from_cache,
        tool_calls=tool_calls,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# The providers, behind the one door (ADR-0028, ADR-0031, ADR-0053)
#
# One class per way of reaching a model, all implementing
# `cascade.llm.providers.ModelProvider`. `LLMClient` holds exactly one and
# never asks it anything the interface does not offer, which is what keeps the
# cache, the meter, the tracer and the record/replay contract above this line
# and provider-agnostic. Everything below this line is the only code in the
# package that reaches a network or a process on a model's behalf.
# ---------------------------------------------------------------------------


class _SdkProvider:
    """Shared machinery for the three providers served by the ``anthropic`` SDK.

    The client is constructed lazily, exactly once, so ``replay`` never builds
    one (:attr:`constructed_sdk_client` is the M0 acceptance instrument).
    Routing is explicit (ADR-0028): every constructor argument comes from
    :func:`~cascade.llm.providers.client_kwargs`, never from the shell.
    """

    name: LLMProvider

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._http_client = http_client
        self._sdk: Any | None = None

    @property
    def spec(self) -> ProviderSpec:
        return spec_for(self.name)

    @property
    def constructed_sdk_client(self) -> bool:
        return self._sdk is not None

    def sdk_client(self) -> Any:
        """Construct the SDK client lazily, exactly once."""
        if self._sdk is not None:
            return self._sdk
        import anthropic  # the single SDK import in the codebase (invariant 5)

        # Re-checked here as well as at construction: a client built in replay
        # mode can still reach this door through `live`-only paths in tests,
        # and the SDK's own error for an empty key -- `TypeError: Could not
        # resolve authentication method` -- names neither the variable nor
        # this project. Measured on a checkout whose .env carried the key with
        # no value.
        problems = readiness_problems(
            self._settings,
            models=(self._settings.models.agent, self._settings.models.compiler),
        )
        if problems:
            raise ProviderNotReady(self.name, problems)

        class_name = self.spec.client_class
        if class_name is None:  # pragma: no cover -- every SDK provider names one
            raise LLMError(f"provider {self.name!r} is not served by an SDK client")
        kwargs = client_kwargs(self._settings)
        if self._http_client is not None:
            kwargs["http_client"] = self._http_client
        self._sdk = getattr(anthropic, class_name)(**kwargs)
        return self._sdk

    def complete(self, request: LLMRequest) -> ProviderResponse:
        """One ``messages.create``, returning the body with the provider's request id.

        Goes through ``with_raw_response`` so the response headers are in
        hand: the Messages API files every call under a `request-id`, AWS
        under `x-amzn-requestid`, and a recording that carries it can be
        matched to the provider's own record of the call (ADR-0053). The body
        is the SDK's own parse of the response, dumped as JSON, exactly as
        before.
        """
        client = self.sdk_client()
        params = _messages_params(request, model=render_model(self._settings, request.model))
        raw = client.messages.with_raw_response.create(**params)
        message = raw.parse()
        body: dict[str, Any] = dict(message.model_dump(mode="json"))
        route = endpoint(self._settings)
        metadata: dict[str, Any] = {"wire_model": params["model"]}
        if route is not None:
            metadata["endpoint"] = route
        return ProviderResponse(
            body=body,
            request_id=_request_id_from_headers(getattr(raw, "headers", None)),
            metadata=metadata,
        )

    def batches(self) -> Any:
        """The SDK's Message Batches resource; refused where the service has none."""
        if not self.spec.supports_batches:
            raise ProviderNotReady(
                self.name,
                [
                    f"{self.spec.operated_by} has no Message Batches API; the batched phases "
                    "need `anthropic` or `claude_platform_aws` (ADR-0028)"
                ],
            )
        return self.sdk_client().messages.batches


class AnthropicApiProvider(_SdkProvider):
    """The Anthropic API, with a pay-as-you-go key -- the pinned configuration."""

    name = "anthropic"


class ClaudePlatformAwsProvider(_SdkProvider):
    """Claude Platform on AWS: Anthropic-operated, reached with IAM/SigV4 (ADR-0028).

    The SDK's ``AnthropicAWS`` client, routed to the configured region and
    workspace and never to an ambient one. Implemented and tested against the
    real SDK over a mock transport; it is **not enabled** in this project yet
    because the account is still being provisioned, and switching to it is
    configuration -- ``LLM_PROVIDER=claude_platform_aws`` and
    ``ANTHROPIC_AWS_WORKSPACE_ID`` -- not code (ADR-0053). It is the
    provider that can carry a batched phase on AWS.
    """

    name = "claude_platform_aws"


class BedrockProvider(_SdkProvider):
    """Amazon Bedrock through the SDK's ``AnthropicBedrockMantle`` client.

    Partner-operated, priced from its own table, and without a Message
    Batches API -- so it serves the unbatched phases and refuses the fan-out
    before any spend (ADR-0028).
    """

    name = "bedrock"


class ClaudeCliProvider:
    """The Claude Code CLI, headless, under the operator's own login (ADR-0031).

    The provider in use while the project runs on a Claude subscription. It
    invokes the locally installed, already-authenticated ``claude`` binary in
    its documented print mode and never touches the subscription's token: the
    CLI authenticates itself. What it cannot do is set ``temperature`` or
    ``max_tokens``, so it records under a namespace of its own and its results
    are labelled as not the pinned configuration wherever they appear.

    Retries only the provider's own transient failures. A 429 is raised at
    once: under a subscription it usually means the plan's usage limit, and
    every phase is resumable (invariant 8), so the honest response is to stop
    and say so rather than spin against the limit.
    """

    name: LLMProvider = "claude_cli"

    def __init__(
        self,
        settings: Settings,
        *,
        runner: CliRunner | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._settings = settings
        self._runner: CliRunner = runner if runner is not None else _run_cli
        self._sleep: Callable[[float], None] = sleep if sleep is not None else time.sleep
        self._workdir: Path | None = None

    @property
    def spec(self) -> ProviderSpec:
        return spec_for(self.name)

    @property
    def constructed_sdk_client(self) -> bool:
        """Always False: the CLI is a process, not an SDK client."""
        return False

    def sdk_client(self) -> Any:
        raise LLMError(f"provider {self.name!r} is not served by an SDK client")

    def batches(self) -> Any:
        raise ProviderNotReady(
            self.name,
            [
                "the Claude Code CLI has no Message Batches API; a subscription bills no "
                "tokens, so resolve the wave serially (`LLMAgents.prepare`, ADR-0052)"
            ],
        )

    def complete(self, request: LLMRequest) -> ProviderResponse:
        """Serve one request through ``claude -p`` and return a Messages body."""
        config = self._settings.providers.claude_cli
        invocation = build_invocation(
            request,
            executable=config.executable,
            model=render_model(self._settings, request.model),
        )
        workdir = self._directory()
        attempts = self._settings.llm.max_retries + 1
        failure = "no attempt made"
        for attempt in range(attempts):
            try:
                done = self._runner(
                    invocation.argv, invocation.stdin, workdir, self._settings.llm.timeout_s
                )
            except FileNotFoundError:
                raise ProviderNotReady(
                    self.name,
                    [
                        f"{config.executable!r} is not on PATH -- install Claude Code and "
                        "log in with the subscription (`claude` then `/login`)"
                    ],
                ) from None
            except OSError as exc:
                # An executable that exists but cannot be run. Measured during
                # the M15 fan-out: a failed Claude Code auto-update left a stub
                # at the install path, and every worker raised ENOEXEC as a raw
                # traceback -- sixty lines each, sixteen at a time -- while the
                # diagnostic written for exactly this situation sat one branch
                # above, unreachable because `FileNotFoundError` is only the
                # *missing* case.
                #
                # Caught after `FileNotFoundError` rather than instead of it:
                # that subclass carries its own remedy ("install it"), and this
                # one carries a different remedy ("it is there and broken"),
                # which is the distinction worth keeping.
                raise ProviderNotReady(
                    self.name,
                    [
                        f"{config.executable!r} exists but could not be executed ({exc}). "
                        "A partial or failed Claude Code update leaves a stub that is "
                        "found on PATH and is not runnable; reinstall it and check "
                        "`claude --version` returns before re-running"
                    ],
                ) from None
            except subprocess.TimeoutExpired:
                failure = f"timed out after {self._settings.llm.timeout_s:.0f}s"
                self._sleep(5.0 * 2**attempt)
                continue
            if not done.stdout.strip():
                raise LLMError(
                    f"claude -p exited {done.returncode} with no result; stderr: "
                    f"{done.stderr.strip()[-400:]!r}. If it is not logged in, run `claude` "
                    "and `/login` with the subscription."
                )
            cli = parse_cli_output(done.stdout)
            status = _status_code(cli.get("api_error_status"))
            if cli.get("is_error") and status == 429:
                raise LLMError(
                    "claude -p hit a rate or usage limit (429). Under a subscription this is "
                    "usually the plan's usage window; the phase is resumable, so re-run the "
                    "same command once the limit resets. Recorded calls are not repeated."
                )
            if cli.get("is_error") and status in _CLI_TRANSIENT_STATUSES:
                failure = f"transient API status {status}"
                self._sleep(5.0 * 2**attempt)
                continue
            if cli.get("is_error") and cli.get("subtype") in _CLI_TRANSIENT_SUBTYPES:
                failure = f"transient CLI subtype {cli.get('subtype')!r}"
                self._sleep(5.0 * 2**attempt)
                continue
            body = to_messages_payload(cli, invocation=invocation, logical_model=request.model)
            return ProviderResponse(
                body=body,
                # The CLI's session is the id its own logs file the call under.
                request_id=_optional_str(cli.get("session_id")) or _optional_str(cli.get("uuid")),
                metadata={"executable": config.executable, "num_turns": cli.get("num_turns")},
            )
        raise LLMError(f"claude -p failed after {attempts} attempt(s): {failure}")

    def _directory(self) -> Path:
        """The working directory the CLI runs in, checked once per provider.

        Preserves the invariant that nothing reaches the model except the
        request. The CLI loads every CLAUDE.md from its working directory
        upward, plus the user's own; this repository's build contract alone is
        ~27k tokens, so a working directory under it would put the contract
        into every forecasting call. Refused rather than warned about.
        """
        if self._workdir is not None:
            return self._workdir
        configured = self._settings.providers.claude_cli.workdir
        path = (
            Path(configured) if configured else Path(tempfile.mkdtemp(prefix="cascade-claude-cli-"))
        ).resolve()
        problems: list[str] = []
        root = repo_root().resolve()
        if path == root or root in path.parents:
            problems.append(f"providers.claude_cli.workdir {path} is inside the repository")
        memories = [
            directory / name
            for directory in (path, *path.parents)
            for name in ("CLAUDE.md", ".claude/CLAUDE.md")
        ]
        memories.append(Path.home() / ".claude" / "CLAUDE.md")
        for memory in sorted(set(memories)):
            if memory.is_file():
                problems.append(f"{memory} would be loaded into every call")
        if problems:
            raise ProviderNotReady(self.name, sorted(problems))
        path.mkdir(parents=True, exist_ok=True)
        self._workdir = path
        return path


def build_provider(
    settings: Settings,
    *,
    http_client: httpx.Client | None = None,
    cli_runner: CliRunner | None = None,
    sleep: Callable[[float], None] | None = None,
) -> ModelProvider:
    """The provider class ``settings.llm.provider`` names, ready to serve.

    Preserves the invariant that a provider is chosen by its canonical name
    and nothing else: aliases were folded at the configuration boundary, so
    this is a four-way dispatch with no default -- a fifth name is a
    programming error here, not a fallback to some provider nobody chose.
    """
    provider = settings.llm.provider
    if provider == "claude_cli":
        return ClaudeCliProvider(settings, runner=cli_runner, sleep=sleep)
    if provider == "anthropic":
        return AnthropicApiProvider(settings, http_client=http_client)
    if provider == "claude_platform_aws":
        return ClaudePlatformAwsProvider(settings, http_client=http_client)
    if provider == "bedrock":
        return BedrockProvider(settings, http_client=http_client)
    raise LLMError(f"no provider class for {provider!r}")  # pragma: no cover -- Literal


class BedrockReranker:
    """Amazon Bedrock's managed reranker, behind the one door (ADR-0047).

    Satisfies :class:`cascade.retrieval.rerank.Reranker` and nothing wider:
    a query, document *bodies*, one score per body, positionally. There is no
    parameter through which it could be told an ``as_of``, a ``chunk_id`` or a
    date, and no return value through which it could name a document the pool
    does not contain -- which is the entire reason ADR-0047 admits a managed
    service here and ADR-0030 refuses one at the filter. Widening this
    signature is what would need a new record.

    It lives in this module for the reason ADR-0030 gave for refusing
    ``ApplyGuardrail``: a second path from somewhere else in the package to a
    model-adjacent service is exactly what invariant 5 exists to prevent.

    **Routing is explicit; identity is ambient** (ADR-0028). The region comes
    from ``providers.bedrock``, never from ``AWS_REGION``, and configured
    endpoint URLs in the environment or the shared config file are ignored --
    botocore still resolves the endpoint, so partitions and FIPS remain its
    problem rather than a template copied into this file. Credentials come
    from the standard AWS chain, because a task role is the point of using IAM.

    **The response is a permutation and is put back in order here.** Bedrock
    answers with ``{index, relevanceScore}`` ranked by relevance, where
    ``index`` points into the request's ``sources`` array. A missing or
    repeated index is an error rather than a gap to fill: a defaulted score is
    a silently reordered piece of evidence, and the ordering *is* the prompt.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        meter: CostMeter | None = None,
        client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._config = settings.retrieval.rerank
        self._bedrock = settings.providers.bedrock
        self._meter = meter
        # Injected for tests, otherwise built on first use. Nothing here
        # imports boto3: the module must cost nothing to import, and `make
        # demo` -- which runs on the local reranker with no AWS account --
        # must never construct one.
        self._client: Any | None = client
        self._price_per_1k = _rerank_price(self._config.price_per_1k_queries)
        # AWS's request ids for the most recent `score` call, one per page, so
        # a recording can be matched to the CloudTrail record of the call.
        self.last_request_ids: tuple[str, ...] = ()

    @property
    def model_id(self) -> str:
        """Identifies the scorer in the cache key and the event log.

        Passed to Bedrock verbatim as ``modelArn``. The field's own published
        pattern makes the ``arn:`` prefix optional, so a bare model id and a
        full ARN are both admissible and the operator chooses; this code
        constructs neither, because an ARN assembled here would have to guess
        a partition and an account it cannot check.
        """
        return self._config.model_id

    @property
    def meter(self) -> CostMeter | None:
        """The meter this reranker books its own queries into, if any."""
        return self._meter

    def score(self, *, query: str, documents: Sequence[str]) -> Sequence[float]:
        """One relevance score per document, higher is better, same order.

        Preserves the protocol's contract that the result describes the whole
        pool: the full set of scores is requested rather than a truncated
        top-N, because ``apply_scores`` is what applies ``top_k`` and a
        response shorter than the pool cannot be a permutation of it.
        """
        bodies = list(documents)
        if not bodies:
            # No call is made, so no query is billed. A reranker with nothing
            # to order must not reach a provider to say so.
            return []
        self._check_pool(bodies)
        scored = self._rerank(self._boto3_client(), query=query, documents=bodies)
        return _in_input_order(scored, expected=len(bodies))

    # -- internals ----------------------------------------------------------

    def _check_pool(self, documents: Sequence[str]) -> None:
        """Refuse a pool Bedrock's own limits cannot describe.

        Checked here rather than left to the service because the alternative
        failure is a ``ValidationException`` naming a JSON path, arriving
        after a round trip that was still billed.
        """
        if len(documents) > _RERANK_MAX_SOURCES:
            raise RerankError(
                f"pool of {len(documents)} exceeds Bedrock's limit of {_RERANK_MAX_SOURCES} "
                "source(s) per Rerank call; a truncated request would score part of the pool "
                "and could not be applied to it"
            )
        for position, body in enumerate(documents):
            if not body:
                raise RerankError(
                    f"document at position {position} is empty; Bedrock requires at least one "
                    "character per source, and a placeholder would be scored as if it were "
                    "the evidence"
                )
            if len(body) > _RERANK_MAX_DOCUMENT_CHARS:
                raise RerankError(
                    f"document at position {position} is {len(body)} characters, over Bedrock's "
                    f"{_RERANK_MAX_DOCUMENT_CHARS}-character limit; truncating it here would "
                    "score text the agent never sees"
                )

    def _boto3_client(self) -> Any:
        """Build the Bedrock client lazily, exactly once, with routing pinned.

        Lazy for the reason :meth:`LLMClient._client` is: a reranker wrapped
        for replay never reaches this method, so a recorded study replays with
        no AWS account at all -- which is also why readiness is asserted here
        and not in ``__init__``.
        """
        if self._client is not None:
            return self._client
        problems = self._readiness_problems()
        if problems:
            raise ProviderNotReady("bedrock", problems)

        import boto3  # lazy by design -- see this method's docstring
        from botocore.config import Config

        session = boto3.session.Session(profile_name=self._bedrock.profile or None)
        self._client = session.client(
            _RERANK_SERVICE,
            # The one place the region is named, so there is one place for it
            # to be wrong. Without it boto3 reads AWS_REGION, and a developer's
            # shell would decide where the study's spend lands (ADR-0028).
            region_name=self._bedrock.region,
            # An explicit endpoint outranks everything; without one botocore
            # resolves the region's own, so partitions and FIPS stay its
            # problem. `ignore_configured_endpoint_urls` is what stops
            # AWS_ENDPOINT_URL and its per-service twin redirecting the call.
            endpoint_url=self._bedrock.base_url or None,
            config=Config(
                ignore_configured_endpoint_urls=True,
                # botocore counts attempts where the SDK counts retries.
                retries={
                    "max_attempts": self._settings.llm.max_retries + 1,
                    "mode": "standard",
                },
                connect_timeout=self._settings.llm.timeout_s,
                read_timeout=self._settings.llm.timeout_s,
            ),
        )
        return self._client

    def _readiness_problems(self) -> list[str]:
        """Everything that stops a paid rerank call being routed and priced.

        The same rule :func:`cascade.llm.providers.readiness_problems` keeps
        for the model providers: no money is spent through a service that
        could not be routed explicitly or priced exactly, and every problem is
        reported at once so the configuration is fixed in one pass.

        A zero price is a problem, not a default. Booking a paid service at
        $0.00 is the shape M8 found in the ledger gate -- a number that agrees
        with itself and cannot fail -- and here it would also spend the phase
        ceiling without moving it.
        """
        problems: list[str] = []
        if not self._bedrock.region:
            problems.append("providers.bedrock.region is not set")
        if not self.model_id.strip():
            problems.append("retrieval.rerank.model_id is not set")
        elif self.model_id == LexicalReranker.model_id:
            problems.append(
                f"retrieval.rerank.model_id is still {LexicalReranker.model_id!r}, the local "
                "reranker's -- name the Bedrock rerank model this study is to be scored against"
            )
        if self._price_per_1k <= 0:
            problems.append(
                "retrieval.rerank.price_per_1k_queries is "
                f"{self._config.price_per_1k_queries!r} -- read the per-query rate from "
                "https://aws.amazon.com/bedrock/pricing/ before a paid run, so the phase "
                "ledger is not a count of free queries"
            )
        return sorted(problems)

    def _request_body(self, *, query: str, documents: Sequence[str]) -> dict[str, Any]:
        """Project one call onto the Rerank request shape.

        ``numberOfResults`` is the pool size rather than the caller's ``k``:
        the published default is unstated, and a response that silently
        returned a top-N would leave the rest of the pool unscored -- which
        `apply_scores` would refuse, correctly, far from the cause.
        """
        return {
            "queries": [{"type": "TEXT", "textQuery": {"text": query}}],
            "sources": [
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": body}},
                }
                for body in documents
            ],
            "rerankingConfiguration": {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": self.model_id},
                    "numberOfResults": len(documents),
                },
            },
        }

    def _rerank(self, client: Any, *, query: str, documents: Sequence[str]) -> dict[int, float]:
        """Call Rerank, following its continuation token, and collect the scores.

        Preserves the one-score-per-document contract across pagination:
        ``nextToken`` is part of this API, so a caller that read only the first
        page would drop scores for a reason invisible in the response it kept.
        A page that returns a token and no new result is a loop and is refused
        rather than followed.

        The query is booked the moment the first response is in hand, not once
        the scores validate: a response this code goes on to refuse was still
        served and still billed. A continuation is part of the same query, so
        it books once however many pages arrive -- at the pool sizes
        ``retrieval.rerank.pool`` permits, bounded above by ``max_k``, one page
        is what is expected. How many search units that query is, is a
        function of the pool size (``_RERANK_DOCUMENTS_PER_QUERY``).
        """
        body = self._request_body(query=query, documents=documents)
        scored: dict[int, float] = {}
        token: str | None = None
        billed = False
        request_ids: list[str] = []

        while True:
            payload = dict(body)
            if token is not None:
                payload["nextToken"] = token
            response: Mapping[str, Any] = client.rerank(**payload)
            request_id = _aws_request_id(response)
            if request_id:
                request_ids.append(request_id)
            self.last_request_ids = tuple(request_ids)
            if not billed:
                self._book_query(documents=len(documents))
                billed = True

            before = len(scored)
            for entry in response.get("results") or []:
                index, value = _rerank_result(entry, expected=len(documents))
                if index in scored:
                    raise RerankError(
                        f"Bedrock returned index {index} twice for a pool of {len(documents)}; "
                        "a response that scores one document twice is not a permutation of the "
                        "pool it was given"
                    )
                scored[index] = value

            token = str(response.get("nextToken") or "") or None
            if token is None:
                return scored
            if len(scored) == before:
                raise RerankError(
                    f"Bedrock returned a continuation token and no new score after "
                    f"{len(scored)} of {len(documents)}; following it again would not terminate"
                )

    def _book_query(self, *, documents: int) -> None:
        """Charge one call to the phase, at the configured per-query rate.

        The request shape carries exactly one query (the field is a
        fixed-size array of 1), which is why the configured rate is per
        thousand *queries* and not per document. But a query "can contain up
        to 100 document chunks" (the service's own pricing page), so a pool of
        150 is two search units, and booking it as one would understate the
        phase by exactly the amount the ledger gate exists to catch.
        """
        if self._meter is None:
            return
        units = max(1, math.ceil(documents / _RERANK_DOCUMENTS_PER_QUERY))
        self._meter.record_units(
            kind=RERANK_BILLING_KIND, units=units, price_per_1k_usd=self._price_per_1k
        )


def _aws_request_id(response: Mapping[str, Any]) -> str | None:
    """boto3's `ResponseMetadata.RequestId`, when the response carries one."""
    metadata = response.get("ResponseMetadata")
    if not isinstance(metadata, Mapping):
        return None
    return _optional_str(metadata.get("RequestId"))


def _rerank_price(configured: str) -> Decimal:
    """Read ``retrieval.rerank.price_per_1k_queries`` as an exact Decimal.

    Parsed once, at construction, so a malformed rate fails before a call
    rather than between the response and the ledger. Never a float: a binary
    fraction of a cent compounds over a study's worth of queries, and the
    meter's whole discipline is exactness.
    """
    try:
        return Decimal(configured)
    except InvalidOperation as exc:
        raise ValueError(
            f"retrieval.rerank.price_per_1k_queries is {configured!r}, which is not a decimal "
            "number; the per-query rate is read as an exact Decimal and cannot be guessed"
        ) from exc


def _rerank_result(entry: Any, *, expected: int) -> tuple[int, float]:
    """Read one ``{index, relevanceScore}`` pair, refusing one that cannot apply.

    Both fields are required by the published response shape, so an entry
    missing either describes something other than this pool. An index outside
    the pool is the failure this check exists for: it would otherwise be
    written into a score vector by position and silently attach one document's
    relevance to another's text.
    """
    if not isinstance(entry, Mapping) or "index" not in entry or "relevanceScore" not in entry:
        raise RerankError(
            f"Bedrock returned a result without an index and a relevanceScore: {entry!r}; "
            "a result that does not say which document it scored cannot be applied"
        )
    index = int(entry["index"])
    if not 0 <= index < expected:
        raise RerankError(
            f"Bedrock returned index {index} for a pool of {expected}; the index names a "
            "position in the request's own source list and this one names nothing"
        )
    return index, float(entry["relevanceScore"])


def _in_input_order(scored: Mapping[int, float], *, expected: int) -> list[float]:
    """Turn indexed results back into one score per document, positionally.

    This is where the permutation is undone. Bedrock ranks its answer by
    relevance, so the order it arrives in is the *result*, not the pool; the
    caller's contract is positional. A missing index is an error rather than a
    default, because a defaulted score sorts and therefore reorders -- and the
    evidence order is the prompt, which is the thing the M8 replay hash is
    taken over.
    """
    missing = [index for index in range(expected) if index not in scored]
    if missing:
        raise RerankError(
            f"Bedrock scored {len(scored)} of {expected} document(s); "
            f"{len(missing)} were never returned (first: {missing[:5]}). A missing score is "
            "not a zero -- defaulting it would silently reorder the evidence"
        )
    return [scored[index] for index in range(expected)]


@dataclass
class RecordedReranker:
    """Record/replay around any reranker (ADR-0047).

    Lives here, beside the one model door, for the reason ADR-0030 gave for
    refusing ``ApplyGuardrail``: a second path that reaches a model-adjacent
    service from somewhere else in the package is exactly what invariant 5
    exists to prevent. ``cascade/retrieval/rerank.py`` stays pure and knows
    nothing about a cache or a network; this wrapper is what makes a remote
    reranker replayable.

    The contract matches :meth:`LLMClient.complete` exactly, because a study
    that can replay its decisions and not its evidence ordering cannot replay
    at all:

    * ``live`` scores every time and records nothing,
    * ``record`` serves a hit from disk and stores a miss,
    * ``replay`` is total with respect to the recorded corpus -- it returns a
      recording or raises :class:`CacheMiss`, and never reaches the inner
      reranker.

    Wrapping a *local* reranker is not pointless: it is how the recorded
    corpus stays complete when the provider is switched, and how a BM25 arm
    and a managed arm are compared under the same machinery rather than one
    of them getting a free pass.

    **Who books the spend.** A hit is booked here, at zero, because this is
    the only place that knows a hit happened; a miss is booked by the inner
    reranker, because that is where a provider is reached and a local one
    costs nothing to reach. The two paths are disjoint -- a hit never consults
    the inner reranker and a miss always does -- so one call is booked exactly
    once, and the same :class:`CostMeter` can be handed to both without
    double-counting.
    """

    inner: Any
    cache: CallCache
    mode: str
    # Optional so a caller that is not measuring spend -- the leakage probes,
    # the property tests -- needs no meter to wrap a reranker.
    meter: CostMeter | None = None
    calls: int = 0
    hits: int = 0

    @property
    def model_id(self) -> str:
        """The inner reranker names itself; this wrapper is not a model."""
        return str(self.inner.model_id)

    def score(self, *, query: str, documents: Sequence[str]) -> Sequence[float]:
        """One score per document, from the recording where there is one."""
        key = rerank_cache_key(model_id=self.model_id, query=query, documents=documents)
        self.calls += 1

        if self.mode != "live":
            recorded = self.cache.get(key)
            if recorded is not None:
                self.hits += 1
                scores = _recorded_scores(recorded, expected=len(documents), key=key)
                # Counted, not ignored: a study replayed end to end would
                # otherwise report no rerank activity at all, and the rerank
                # hit rate is what says whether the recorded corpus covers the
                # pools this run actually asked for.
                if self.meter is not None:
                    self.meter.record_unit_cache_hit(kind=RERANK_BILLING_KIND)
                return scores
            if self.mode == "replay":
                raise CacheMiss(
                    f"no recorded rerank for key {key} (model={self.model_id!r}, "
                    f"{len(documents)} document(s)) in {self.cache.root}. Replay never "
                    "falls back to a reranker -- re-record with CASCADE_LLM__MODE=record "
                    "if this pool is new."
                )

        started = time.perf_counter()
        scores = [float(value) for value in self.inner.score(query=query, documents=documents)]
        latency_ms = (time.perf_counter() - started) * 1000.0

        if self.mode == "record":
            request_ids = tuple(getattr(self.inner, "last_request_ids", ()) or ())
            self.cache.put(
                CachedCall(
                    key=key,
                    # Enough to identify the call without storing the pool
                    # twice: the bodies are already in the key, and a
                    # recording that repeated them would be megabytes per
                    # query for no lookup that needs them.
                    request_digest={
                        "kind": "rerank",
                        "model_id": self.model_id,
                        "query": query,
                        "documents": len(documents),
                    },
                    raw_response={"scores": scores},
                    # Reranking is not token-billed -- the managed services
                    # price per query -- so a token usage here would be a
                    # fiction the meter would then sum. It is recorded as zero
                    # and the query count is what a spend reconciliation reads.
                    usage=Usage(input_tokens=0, output_tokens=0),
                    latency_ms=latency_ms,
                    recorded_at=datetime.now(UTC).isoformat(),
                    provider_metadata={
                        "kind": "rerank",
                        "model_id": self.model_id,
                        "request_ids": list(request_ids),
                    },
                )
            )
        return scores


def _recorded_scores(recorded: CachedCall, *, expected: int, key: str) -> list[float]:
    """Read scores out of a recording, refusing one that cannot be applied.

    A recording whose length disagrees with the pool is a corrupted or
    mis-keyed entry, and returning it would let `apply_scores` raise far from
    the cause. Checked here, where the key is still in hand.
    """
    raw = recorded.raw_response.get("scores")
    if not isinstance(raw, list) or len(raw) != expected:
        raise RerankError(
            f"recorded rerank {key} holds {len(raw) if isinstance(raw, list) else 'no'} "
            f"score(s) for a pool of {expected}; the recording does not describe this call"
        )
    return [float(value) for value in raw]


@dataclass(frozen=True, slots=True)
class BedrockGuardrail:
    """``ApplyGuardrail`` through boto3 -- the only thing here that egresses.

    Here, and not in ``cascade/eval/``, because ADR-0030's objection to
    guardrails was precisely that ``ApplyGuardrail`` is a second
    model-adjacent path outside this module -- and that objection is not
    answered by putting it in a different package. It sits beside
    ``RecordedReranker``, which is here for the same reason (ADR-0047), and
    behind :class:`~cascade.llm.types.GuardrailScreen`, so the audit that uses
    it imports a protocol and never this class.

    Invariant 5 is enforced for this by ``tests/unit/test_invariants.py``,
    which now refuses a boto3 client for a model-serving service anywhere but
    here; observability and database clients are unaffected.

    Routing is explicit and identity is ambient (ADR-0028): the region,
    profile and endpoint come from ``providers.bedrock``, and credentials come
    from the standard AWS chain. A stray ``AWS_REGION`` in someone's shell
    cannot decide where this call lands.

    The request shape is read from the installed botocore service model for
    ``bedrock-runtime`` rather than from memory: ``guardrailIdentifier``,
    ``guardrailVersion``, ``source`` and ``content`` are its four required
    members, and ``content`` is a list of tagged unions whose text arm is
    ``{"text": {"text": ...}}``.
    """

    client: Any
    guardrail_id: str
    guardrail_version: str

    @classmethod
    def from_settings(cls, settings: Settings) -> BedrockGuardrail:
        """Build from ``providers.bedrock``, or refuse by naming what is missing.

        Refuses rather than defaulting. A guardrail id guessed from a partial
        configuration would produce an audit of something nobody chose, and
        ``ResourceNotFoundException`` on 180 graphs reads as an outage rather
        than as a configuration error.
        """
        bedrock = settings.providers.bedrock
        missing = sorted(
            name
            for name, value in (
                ("providers.bedrock.guardrail_id", bedrock.guardrail_id),
                ("providers.bedrock.guardrail_version", bedrock.guardrail_version),
                ("providers.bedrock.region", bedrock.region),
            )
            if not value
        )
        if missing:
            raise ValueError("cannot reach a Bedrock Guardrail: " + ", ".join(missing) + " not set")
        import boto3

        session = boto3.session.Session(
            profile_name=bedrock.profile or None,
            region_name=bedrock.region,
        )
        # `providers.bedrock.base_url` is deliberately NOT passed as
        # `endpoint_url`. It is the Anthropic-compatible Mantle endpoint
        # (`bedrock-mantle.{region}.api.aws/anthropic`, `llm/providers.py`),
        # which serves the Messages API and not `ApplyGuardrail`; handing it to
        # a `bedrock-runtime` client would send every screening request to a
        # service that does not implement the operation, and the resulting
        # 404s would arrive in the audit as 180 errors -- an outage, which is
        # the one verdict hardest to tell from a configuration mistake.
        # Routing stays explicit because the region is passed explicitly and
        # the endpoint is a documented function of it (ADR-0028).
        return cls(
            client=session.client("bedrock-runtime", region_name=bedrock.region),
            guardrail_id=str(bedrock.guardrail_id),
            guardrail_version=str(bedrock.guardrail_version),
        )

    def screen(self, *, text: str) -> ScreenResult:
        """One ``ApplyGuardrail`` call. Raises on anything but an answer.

        Preserves the rule that an unanswered graph is unassessed: every
        failure propagates to :func:`audit_graphs`, which records it as an
        error rather than as a clean result. An unrecognised ``action`` also
        raises, because the two documented values are the audit's whole
        vocabulary and a third one silently read as NONE would understate a
        confound.
        """
        response = self.client.apply_guardrail(
            guardrailIdentifier=self.guardrail_id,
            guardrailVersion=self.guardrail_version,
            source=SOURCE,
            content=[{"text": {"text": text}}],
        )
        action = str(response.get("action", ""))
        if action not in ("NONE", "GUARDRAIL_INTERVENED"):
            raise ValueError(
                f"ApplyGuardrail returned action {action!r}, which this audit does not "
                "recognise; treating it as 'not flagged' would understate a confound"
            )
        # The assessment object's policy members are the six the service model
        # names, all suffixed "Policy"; its other members (`invocationMetrics`,
        # `appliedGuardrailDetails`) are bookkeeping and would read as policies
        # that fired.
        assessments = response.get("assessments") or []
        policies = sorted(
            {
                str(key)
                for assessment in assessments
                if isinstance(assessment, dict)
                for key in sorted(assessment)
                if str(key).endswith("Policy") and assessment.get(key)
            }
        )
        return ScreenResult(
            action="GUARDRAIL_INTERVENED" if action == "GUARDRAIL_INTERVENED" else "NONE",
            reason=str(response.get("actionReason") or ""),
            policies=tuple(policies),
            request_id=_aws_request_id(response) or "",
        )


# ---------------------------------------------------------------------------
# `cascade aws check`: the AWS surfaces, read without spending (ADR-0053)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AwsCheck:
    """One read-only check: what was asked, whether it held, and what came back."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class AwsAccessReport:
    """What `describe_aws_access` found. ``ok`` only when every check held."""

    region: str | None
    checks: tuple[AwsCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def describe_aws_access(settings: Settings, *, session: Any | None = None) -> AwsAccessReport:
    """Who the ambient AWS identity is, and whether the configured surfaces resolve.

    Three control-plane reads and nothing else -- ``sts:GetCallerIdentity``,
    ``bedrock:GetGuardrail`` for the configured guardrail, and
    ``bedrock:GetFoundationModel`` for the configured reranker -- so it
    invokes no model, screens no text, bills nothing and changes nothing.
    Preserves ADR-0028's routing rule: every client is built for
    ``providers.bedrock.region``, never for an ambient ``AWS_REGION``, and
    credentials stay ambient. Lives here because a ``bedrock`` client is a
    model-serving service's client and may be built nowhere else
    (invariant 5, ``tests/unit/test_invariants.py``).

    Failures are reported per check, by the exception class AWS raised, so an
    operator sees "no credentials" beside "the guardrail resolves" rather than
    one traceback. Only botocore's own exceptions are caught; anything else is
    a bug and propagates.
    """
    bedrock = settings.providers.bedrock
    region = bedrock.region
    checks: list[AwsCheck] = []
    if not region:
        checks.append(
            AwsCheck(
                "region",
                False,
                "providers.bedrock.region is not set; the AWS surfaces are routed nowhere",
            )
        )
        return AwsAccessReport(region=None, checks=tuple(checks))
    checks.append(AwsCheck("region", True, region))

    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        checks.append(
            AwsCheck("boto3", False, "boto3 is not installed: `uv sync --extra aws` (ADR-0028)")
        )
        return AwsAccessReport(region=region, checks=tuple(checks))

    if session is None:
        session = boto3.session.Session(profile_name=bedrock.profile or None)
    config = Config(
        ignore_configured_endpoint_urls=True,
        retries={"max_attempts": 2, "mode": "standard"},
        connect_timeout=10,
        read_timeout=20,
    )

    try:
        identity = session.client("sts", region_name=region, config=config).get_caller_identity()
    except (BotoCoreError, ClientError) as exc:
        checks.append(AwsCheck("identity", False, f"{type(exc).__name__}: {exc}"))
        # Without an identity nothing else can be asked; say so once.
        return AwsAccessReport(region=region, checks=tuple(checks))
    checks.append(
        AwsCheck(
            "identity",
            True,
            f"account {identity.get('Account', '?')}, principal {identity.get('Arn', '?')}",
        )
    )

    control_plane = session.client("bedrock", region_name=region, config=config)

    if bedrock.guardrail_id:
        try:
            guardrail = control_plane.get_guardrail(
                guardrailIdentifier=bedrock.guardrail_id,
                guardrailVersion=bedrock.guardrail_version or "DRAFT",
            )
        except (BotoCoreError, ClientError) as exc:
            checks.append(AwsCheck("guardrail", False, f"{type(exc).__name__}: {exc}"))
        else:
            checks.append(
                AwsCheck(
                    "guardrail",
                    True,
                    f"{guardrail.get('name', '?')} ({bedrock.guardrail_id} version "
                    f"{guardrail.get('version', bedrock.guardrail_version or 'DRAFT')}), "
                    f"status {guardrail.get('status', '?')}",
                )
            )
    else:
        checks.append(
            AwsCheck(
                "guardrail",
                True,
                "not configured -- set CASCADE_GUARDRAIL_ID and CASCADE_GUARDRAIL_VERSION "
                "for `cascade eval guardrails` (ADR-0050)",
            )
        )

    rerank = settings.retrieval.rerank
    if rerank.provider == "bedrock":
        try:
            model = control_plane.get_foundation_model(modelIdentifier=rerank.model_id)
        except (BotoCoreError, ClientError) as exc:
            checks.append(AwsCheck("rerank model", False, f"{type(exc).__name__}: {exc}"))
        else:
            details = model.get("modelDetails") or {}
            checks.append(
                AwsCheck(
                    "rerank model",
                    True,
                    f"{details.get('modelName', rerank.model_id)} ({rerank.model_id}), "
                    f"lifecycle {(details.get('modelLifecycle') or {}).get('status', '?')}",
                )
            )
    else:
        checks.append(
            AwsCheck(
                "rerank model",
                True,
                f"local reranker ({rerank.model_id}); Bedrock Rerank is opt-in through "
                "retrieval.rerank.provider=bedrock (ADR-0047)",
            )
        )
    return AwsAccessReport(region=region, checks=tuple(checks))

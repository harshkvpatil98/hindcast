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

Four providers can sit behind this door (ADR-0028, ADR-0031): Anthropic
directly, Claude Platform on AWS, Amazon Bedrock, and the Claude Code CLI run
headless under a subscription. The three SDK client classes live in the one
``anthropic`` package, so this module is still the only importer of it, and it
is also the only place the CLI is run -- so every call, whichever provider
serves it, is cached, metered and traced the same way. What differs between
them is described in :mod:`cascade.llm.providers`, which is pure.
"""

from __future__ import annotations

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
from cascade.config import Settings, claude_cli_environment, repo_root
from cascade.llm.cache import CallCache, cache_domain, cache_key
from cascade.llm.claude_cli import build_invocation, parse_cli_output, to_messages_payload
from cascade.llm.meter import CostMeter
from cascade.llm.providers import client_kwargs, readiness_problems, render_model, spec_for
from cascade.llm.tracing import Tracer, null_tracer
from cascade.llm.types import (
    BatchFailed,
    BatchItem,
    CachedCall,
    CacheMiss,
    LLMError,
    LLMRequest,
    LLMResult,
    PromptTooShortToCache,
    ProviderNotReady,
    Usage,
)
from cascade.retrieval.rerank import LexicalReranker, RerankError, rerank_cache_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

__all__ = [
    "BATCH_MAX_REQUESTS",
    "RERANK_BILLING_KIND",
    "BedrockReranker",
    "CliCompleted",
    "CliRunner",
    "LLMClient",
    "RecordedReranker",
    "assert_cacheable_prefix",
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
        self._http_client = http_client
        self._sdk: Any | None = None
        self._provider = settings.llm.provider
        # None for the three API providers, which share one namespace
        # (ADR-0029); the CLI gets its own (ADR-0031).
        self._namespace = spec_for(self._provider).cache_namespace
        self._cli_runner: CliRunner = cli_runner if cli_runner is not None else _run_cli
        self._cli_workdir: Path | None = None

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
    def constructed_sdk_client(self) -> bool:
        """Whether an SDK client has ever been built in this process.

        The M0 acceptance criterion is that replay makes zero network calls.
        A transport that raises proves no call was *sent*; this proves none
        could have been, because no client exists to send one.
        """
        return self._sdk is not None

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
        if not spec_for(self._provider).supports_batches:
            raise ProviderNotReady(
                self._provider,
                [
                    f"{len(pending)} uncached request(s) need the Message Batches API, which "
                    f"{spec_for(self._provider).operated_by} does not provide; the batched "
                    "phase's ceiling assumes the batch rate (ADR-0020). Record this phase "
                    "through `anthropic` or `aws` -- the recordings then replay through any "
                    "provider (ADR-0029)"
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
        client = self._client()
        payload = [
            {
                "custom_id": _batch_id(key),
                "params": _batch_params(
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
        batch = client.messages.batches.create(requests=payload)
        batch_id = str(getattr(batch, "id", ""))
        status = str(getattr(batch, "processing_status", ""))
        while status != "ended":
            if time.perf_counter() - started > timeout_s:
                raise LLMError(
                    f"batch {batch_id} still {status!r} after {timeout_s:.0f}s; "
                    "the provider's own SLA is 24h -- raise the timeout or cancel it"
                )
            self._sleep(poll_interval_s)
            batch = client.messages.batches.retrieve(batch_id)
            status = str(getattr(batch, "processing_status", ""))

        out: dict[str, LLMResult] = {}
        failures: dict[str, str] = {}
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        by_batch_id = {_batch_id(key): key for key in sorted(requests)}

        for entry in client.messages.batches.results(batch_id):
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
            )
            out[key] = result

        if failures:
            raise BatchFailed(failures)
        return out

    def _sleep(self, seconds: float) -> None:
        """Wait between polls. Isolated so a test can drive the loop instantly."""
        time.sleep(seconds)

    # -- the CLI provider (ADR-0031) ------------------------------------------

    def _call_cli(self, request: LLMRequest) -> dict[str, Any]:
        """Serve one request through ``claude -p`` and return a Messages body.

        Retries only the provider's own transient failures. A 429 is raised at
        once: under a subscription it usually means the plan's usage limit,
        and every phase is resumable (invariant 8), so the honest response is
        to stop and say so rather than spin against the limit.
        """
        config = self._settings.providers.claude_code
        invocation = build_invocation(
            request,
            executable=config.executable,
            model=render_model(self._settings, request.model),
        )
        workdir = self._cli_directory()
        attempts = self._settings.llm.max_retries + 1
        failure = "no attempt made"
        for attempt in range(attempts):
            try:
                done = self._cli_runner(
                    invocation.argv, invocation.stdin, workdir, self._settings.llm.timeout_s
                )
            except FileNotFoundError:
                raise ProviderNotReady(
                    "claude_code",
                    [
                        f"{config.executable!r} is not on PATH -- install Claude Code and "
                        "log in with the subscription (`claude` then `/login`)"
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
            return to_messages_payload(cli, invocation=invocation, logical_model=request.model)
        raise LLMError(f"claude -p failed after {attempts} attempt(s): {failure}")

    def _cli_directory(self) -> Path:
        """The working directory the CLI runs in, checked once per client.

        Preserves the invariant that nothing reaches the model except the
        request. The CLI loads every CLAUDE.md from its working directory
        upward, plus the user's own; this repository's build contract alone is
        ~27k tokens, so a working directory under it would put the contract
        into every forecasting call. Refused rather than warned about.
        """
        if self._cli_workdir is not None:
            return self._cli_workdir
        configured = self._settings.providers.claude_code.workdir
        path = (
            Path(configured) if configured else Path(tempfile.mkdtemp(prefix="cascade-claude-cli-"))
        ).resolve()
        problems: list[str] = []
        root = repo_root().resolve()
        if path == root or root in path.parents:
            problems.append(f"providers.claude_code.workdir {path} is inside the repository")
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
            raise ProviderNotReady("claude_code", sorted(problems))
        path.mkdir(parents=True, exist_ok=True)
        self._cli_workdir = path
        return path

    # -- internals ----------------------------------------------------------

    def _from_recording(self, recorded: CachedCall, *, trace_name: str) -> LLMResult:
        """Rebuild a result from disk and book it as a zero-cost generation.

        Cache hits are traced rather than skipped (spec §12.2): hit rate and
        spend have to appear on the same dashboard, or the biggest cost lever
        in the study is invisible.
        """
        result = _result_from_payload(
            recorded.raw_response,
            usage=recorded.usage,
            latency_ms=recorded.latency_ms,
            served_from_cache=True,
        )
        self.meter.record_cache_hit(model=result.model, usage=result.usage)
        self._tracer.generation(
            name=trace_name,
            model=result.model,
            usage=result.usage,
            cost_usd=None,
            cached=True,
            latency_ms=recorded.latency_ms,
        )
        return result

    def _client(self) -> Any:
        """Construct the SDK client lazily, exactly once.

        Lazy so that ``replay`` never builds one -- see
        :attr:`constructed_sdk_client`.
        """
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
            raise ProviderNotReady(self._provider, problems)

        class_name = spec_for(self._provider).client_class
        if class_name is None:
            raise LLMError(f"provider {self._provider!r} is not served by an SDK client")
        kwargs = client_kwargs(self._settings)
        if self._http_client is not None:
            kwargs["http_client"] = self._http_client
        self._sdk = getattr(anthropic, class_name)(**kwargs)
        return self._sdk

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
        if self._provider == "claude_code":
            raw = self._call_cli(request)
        else:
            client = self._client()
            payload: dict[str, Any] = {
                "model": render_model(self._settings, request.model),
                "messages": request.messages,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
            }
            if request.system is not None:
                payload["system"] = request.system
            if request.tools:
                payload["tools"] = request.tools
            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice
            message = client.messages.create(**payload)
            raw = message.model_dump(mode="json")
        latency_ms = (time.perf_counter() - started) * 1000.0

        usage = _usage_from_payload(raw)
        result = _result_from_payload(
            raw, usage=usage, latency_ms=latency_ms, served_from_cache=False
        )

        cost = self.meter.record(model=request.model, usage=usage, batch=batch)
        self._tracer.generation(
            name=trace_name,
            model=result.model,
            usage=usage,
            cost_usd=cost,
            cached=False,
            latency_ms=latency_ms,
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


def _batch_params(request: LLMRequest, *, model: str) -> dict[str, Any]:
    """Project a request onto the Batches API's per-item ``params`` object.

    ``model`` is the provider's wire id, rendered by the caller; the request
    itself only ever carries the logical model (ADR-0029).
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
    )


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
        is what is expected.
        """
        body = self._request_body(query=query, documents=documents)
        scored: dict[int, float] = {}
        token: str | None = None
        billed = False

        while True:
            payload = dict(body)
            if token is not None:
                payload["nextToken"] = token
            response: Mapping[str, Any] = client.rerank(**payload)
            if not billed:
                self._book_one_query()
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

    def _book_one_query(self) -> None:
        """Charge one query to the phase, at the configured per-query rate.

        One call is one query: the request shape carries exactly one query
        (the field is a fixed-size array of 1), whatever the pool size, which
        is why the configured rate is per thousand *queries* and not per
        document.
        """
        if self._meter is None:
            return
        self._meter.record_units(
            kind=RERANK_BILLING_KIND, units=1, price_per_1k_usd=self._price_per_1k
        )


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

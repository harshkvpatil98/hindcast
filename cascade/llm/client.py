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

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

__all__ = [
    "BATCH_MAX_REQUESTS",
    "CliCompleted",
    "CliRunner",
    "LLMClient",
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

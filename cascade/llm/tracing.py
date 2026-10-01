"""Observability wiring: a trace per run, a span per step, a generation per call.

Two sinks share one interface. :class:`LangfuseTracer` reports to the
self-hosted Langfuse (ADR-0006); :class:`CallLogTracer` appends one JSON
object per call to a local file (ADR-0053) -- the provider, the model, the
provider's own request id, the token counts, the cost and whether the cache
served it -- so a call can be matched to the provider's record of it
(CloudTrail, Bedrock's invocation logs, the CLI's session) without a Langfuse
instance in the loop. :class:`CompositeTracer` fans out to both.

This module holds the **one sanctioned broad exception guard** in the codebase
(CLAUDE.md §4). Observability must never fail a run: a 36,000-run phase that
dies because a telemetry sidecar restarted has cost real money for nothing.
Every guard here is annotated, narrow in scope, and covered by a test that
asserts the tracer degrades to a no-op rather than propagating.

The rule that makes this safe is that nothing downstream reads back from a
tracer during a run. The cost ledger is authoritative and lives in Postgres;
Langfuse is reconciled *against* it at M8, so a dropped span costs a
reconciliation warning, never a wrong number.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from cascade.config import Settings
from cascade.llm.types import Usage

__all__ = [
    "CallLogTracer",
    "CompositeTracer",
    "LangfuseTracer",
    "NullTracer",
    "Tracer",
    "build_tracer",
    "null_tracer",
]


class Tracer:
    """No-op tracer. Also the base class, so the null path is the tested path.

    Subclasses override the three hooks. Anything that fails inside an
    override must be swallowed there, not here.
    """

    enabled: bool = False
    degraded_reason: str | None = None

    @contextmanager
    def run(self, *, run_id: str, metadata: dict[str, Any] | None = None) -> Iterator[None]:
        """Open a trace for one simulation run."""
        del run_id, metadata
        yield

    @contextmanager
    def step(self, *, index: int, metadata: dict[str, Any] | None = None) -> Iterator[None]:
        """Open a span for one step of the 24-step loop."""
        del index, metadata
        yield

    def generation(
        self,
        *,
        name: str,
        model: str,
        usage: Usage,
        cost_usd: Decimal | None,
        cached: bool,
        latency_ms: float,
        provider: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """Log one model call.

        Cache hits arrive here with ``cost_usd=None`` and ``cached=True`` and
        are logged as zero-cost generations, so hit rate and spend appear on
        the same dashboard (spec §12.2). ``provider`` and ``request_id`` are
        the provider's identity for the call (ADR-0053); both are optional so
        every existing caller keeps its signature.
        """

    def flush(self) -> None:
        """Push buffered events. Safe to call when nothing is buffered."""


class NullTracer(Tracer):
    """Explicit name for the disabled tracer, for readable diagnostics."""

    def __init__(self, reason: str | None = None) -> None:
        self.degraded_reason = reason


def null_tracer() -> Tracer:
    """Return a tracer that records nothing."""
    return NullTracer()


class LangfuseTracer(Tracer):
    """Reports to a self-hosted Langfuse v2 instance (ADR-0006)."""

    enabled = True

    def __init__(self, client: Any) -> None:
        self._client = client
        self._trace: Any | None = None
        self._span: Any | None = None

    @contextmanager
    def run(self, *, run_id: str, metadata: dict[str, Any] | None = None) -> Iterator[None]:
        try:
            self._trace = self._client.trace(id=run_id, name="cascade.run", metadata=metadata)
        except Exception as exc:  # noqa: BLE001 -- sanctioned: telemetry never fails a run
            self._trace = None
            self.degraded_reason = f"trace open failed: {type(exc).__name__}"
        try:
            yield
        finally:
            self._trace = None

    @contextmanager
    def step(self, *, index: int, metadata: dict[str, Any] | None = None) -> Iterator[None]:
        parent = self._trace
        if parent is not None:
            try:
                self._span = parent.span(name=f"step.{index:02d}", metadata=metadata)
            except Exception as exc:  # noqa: BLE001 -- sanctioned: see module docstring
                self._span = None
                self.degraded_reason = f"span open failed: {type(exc).__name__}"
        try:
            yield
        finally:
            span, self._span = self._span, None
            if span is not None:
                try:
                    span.end()
                except Exception as exc:  # noqa: BLE001 -- sanctioned: see module docstring
                    self.degraded_reason = f"span end failed: {type(exc).__name__}"

    def generation(
        self,
        *,
        name: str,
        model: str,
        usage: Usage,
        cost_usd: Decimal | None,
        cached: bool,
        latency_ms: float,
        provider: str | None = None,
        request_id: str | None = None,
    ) -> None:
        parent = self._span or self._trace or self._client
        try:
            parent.generation(
                name=name,
                model=model,
                usage={
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                    "unit": "TOKENS",
                    # A cache hit is a real generation that cost nothing. Logging
                    # it as 0.0 rather than omitting it keeps the dashboard's
                    # call count equal to the study's call count.
                    "totalCost": float(cost_usd) if cost_usd is not None else 0.0,
                },
                metadata={
                    "cached": cached,
                    "latency_ms": latency_ms,
                    "cache_read_input_tokens": usage.cache_read_input_tokens,
                    "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                    "provider": provider,
                    "request_id": request_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 -- sanctioned: see module docstring
            self.degraded_reason = f"generation failed: {type(exc).__name__}"

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:  # noqa: BLE001 -- sanctioned: see module docstring
            self.degraded_reason = f"flush failed: {type(exc).__name__}"


class CallLogTracer(Tracer):
    """One JSON object per call, appended to a local file (ADR-0053).

    The structured record of what reached a provider: enough to reconcile a
    phase against the provider's own logs by request id, and to see at a
    glance which provider served a run and how much of it the cache answered.
    Append-only, one line per event, so a crashed phase leaves a readable
    prefix and a resumed one continues it.

    Writes are guarded like Langfuse's: a full disk or a read-only mount must
    degrade the log, never the run. The file is opened per event rather than
    held, so a process that forks workers does not interleave half-lines.
    """

    enabled = True

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._run_id: str | None = None
        self._step: int | None = None

    @contextmanager
    def run(self, *, run_id: str, metadata: dict[str, Any] | None = None) -> Iterator[None]:
        self._run_id = run_id
        self._append({"event": "run.start", "run_id": run_id, "metadata": metadata})
        try:
            yield
        finally:
            self._append({"event": "run.end", "run_id": run_id})
            self._run_id = None

    @contextmanager
    def step(self, *, index: int, metadata: dict[str, Any] | None = None) -> Iterator[None]:
        del metadata
        self._step = index
        try:
            yield
        finally:
            self._step = None

    def generation(
        self,
        *,
        name: str,
        model: str,
        usage: Usage,
        cost_usd: Decimal | None,
        cached: bool,
        latency_ms: float,
        provider: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self._append(
            {
                "event": "generation",
                "name": name,
                "run_id": self._run_id,
                "step": self._step,
                "provider": provider,
                "model": model,
                "request_id": request_id,
                "cached": cached,
                "latency_ms": round(latency_ms, 3),
                # A string, never a float: the ledger's whole discipline is
                # exactness, and a log that rounded differently from the meter
                # would disagree with it by construction.
                "cost_usd": str(cost_usd) if cost_usd is not None else "0",
                "usage": usage.model_dump(mode="json"),
            }
        )

    def _append(self, record: dict[str, Any]) -> None:
        record = {"ts": datetime.now(UTC).isoformat(), **record}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 -- sanctioned: telemetry never fails a run
            self.degraded_reason = f"call log write failed: {type(exc).__name__}"


class CompositeTracer(Tracer):
    """Fans every event out to several tracers; one failing never silences another."""

    enabled = True

    def __init__(self, tracers: list[Tracer]) -> None:
        self.tracers = list(tracers)

    @property
    def degraded_reason(self) -> str | None:
        reasons = [t.degraded_reason for t in self.tracers if t.degraded_reason]
        return "; ".join(reasons) if reasons else None

    @degraded_reason.setter
    def degraded_reason(self, value: str | None) -> None:
        # The members carry their own reasons; the composite only reports them.
        del value

    @contextmanager
    def run(self, *, run_id: str, metadata: dict[str, Any] | None = None) -> Iterator[None]:
        from contextlib import ExitStack

        with ExitStack() as stack:
            for tracer in self.tracers:
                stack.enter_context(tracer.run(run_id=run_id, metadata=metadata))
            yield

    @contextmanager
    def step(self, *, index: int, metadata: dict[str, Any] | None = None) -> Iterator[None]:
        from contextlib import ExitStack

        with ExitStack() as stack:
            for tracer in self.tracers:
                stack.enter_context(tracer.step(index=index, metadata=metadata))
            yield

    def generation(
        self,
        *,
        name: str,
        model: str,
        usage: Usage,
        cost_usd: Decimal | None,
        cached: bool,
        latency_ms: float,
        provider: str | None = None,
        request_id: str | None = None,
    ) -> None:
        for tracer in self.tracers:
            tracer.generation(
                name=name,
                model=model,
                usage=usage,
                cost_usd=cost_usd,
                cached=cached,
                latency_ms=latency_ms,
                provider=provider,
                request_id=request_id,
            )

    def flush(self) -> None:
        for tracer in self.tracers:
            tracer.flush()


def build_tracer(settings: Settings) -> Tracer:
    """Return the configured tracers, or a no-op that says why it is one.

    Preserves the invariant that observability is optional at runtime and
    mandatory in configuration: a missing key degrades to a no-op with a
    stated reason, so ``doctor`` can report it, rather than silently
    pretending tracing is on. The call log (``observability.call_log``) is
    independent of Langfuse: either, both or neither.
    """
    sinks: list[Tracer] = []
    reasons: list[str] = []

    if not settings.langfuse.enabled:
        reasons.append("disabled in config")
    elif settings.langfuse_public_key is None or settings.langfuse_secret_key is None:
        reasons.append("langfuse keys not set in the environment")
    else:
        try:
            from langfuse import Langfuse  # optional dependency, probed rather than required

            client = Langfuse(
                public_key=settings.langfuse_public_key.get_secret_value(),
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                host=settings.langfuse.host,
            )
        except Exception as exc:  # noqa: BLE001 -- sanctioned: telemetry never fails a run
            reasons.append(f"langfuse unavailable: {type(exc).__name__}: {exc}"[:160])
        else:
            sinks.append(LangfuseTracer(client))

    if settings.observability.call_log:
        sinks.append(CallLogTracer(settings.call_log_path()))

    if not sinks:
        return NullTracer("; ".join(reasons) or "no tracer configured")
    if len(sinks) == 1:
        return sinks[0]
    return CompositeTracer(sinks)

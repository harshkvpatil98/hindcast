"""Token and USD accounting with a hard per-phase ceiling (spec §12.4).

Every figure here is :class:`~decimal.Decimal`. Binary floats would make the
6-decimal-place acceptance criterion fail for reasons unrelated to the meter,
and the M8 ledger reconciliation (±2% against Langfuse) has no tolerance to
spare for accumulated representation error over ~378,000 calls.

A breach is never a warning. The meter writes a resumable checkpoint and
raises :class:`BudgetExceeded`, which the CLI boundary maps to exit code 2.

**Not everything in a phase is billed per token.** A managed reranker bills
per query (ADR-0047), and pricing it through the token table would mean
inventing a token count -- whose only honest value, zero, prices every query
at nothing. Per-call work is therefore booked by :meth:`CostMeter.record_units`
into counters of its own: it spends the same phase ceiling, and it is kept out
of ``calls`` so the action-cache hit rate the M6 criterion reads keeps meaning
what it measured.

**The ceiling belongs to the phase, not to the process.** Until M12 a meter
started every process at zero and nothing read a checkpoint back, so a phase
that aborted at its ceiling and was re-run got the whole ceiling again -- a
retry loop around `simulate all` would have spent $240 per attempt. A meter now
starts from the phase's recorded spend, and records it as it goes (at every
percent of the ceiling, so a crash loses at most that much accounting). Re-
running a breached phase therefore breaches again at its first paid call;
going further takes a deliberate act -- raising the ceiling in configuration,
or deleting the checkpoint to declare a new study.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from cascade.config import CacheTTL, PricingEntry, PricingMultipliers, Settings
from cascade.llm.types import BudgetExceeded, Usage

__all__ = [
    "USD",
    "CostMeter",
    "PhaseEstimate",
    "compute_cost",
    "compute_unit_cost",
    "estimate_phase",
    "quantize_usd",
]

_PER_MTOK = Decimal(1_000_000)
# Per-unit prices are quoted per thousand calls, not per million tokens: a
# managed reranker bills per query (ADR-0047) and `retrieval.rerank
# .price_per_1k_queries` is written that way.
_PER_KILO = Decimal(1_000)
USD = Decimal("0.000001")


def compute_unit_cost(*, units: int, price_per_1k_usd: Decimal) -> Decimal:
    """Return the exact USD cost of ``units`` calls to a per-call-billed service.

    Preserves the rule that nothing is priced through a table it does not
    belong to. A managed reranker bills per query rather than per token
    (ADR-0047), so running it through :func:`compute_cost` would need a token
    count that does not exist -- and the honest zero, ``Usage(0, 0)``, prices
    every query at nothing, which is the M8 failure of a gate that compares
    two zeros.

    The result is exact, not rounded -- see :func:`quantize_usd`.
    """
    if units < 0:
        raise ValueError(f"cannot price {units} units: a negative count is not a call")
    if price_per_1k_usd < 0:
        raise ValueError(f"cannot price at {price_per_1k_usd} per 1k: a rate is never negative")
    return Decimal(units) * price_per_1k_usd / _PER_KILO


def quantize_usd(value: Decimal) -> Decimal:
    """Round to the reporting precision, six decimal places.

    Used only at the boundary. Internal accumulation stays exact so that
    rounding is applied once, not once per call.
    """
    return value.quantize(USD)


def compute_cost(
    *,
    usage: Usage,
    price: PricingEntry,
    multipliers: PricingMultipliers,
    batch: bool,
    cache_ttl: CacheTTL,
) -> Decimal:
    """Return the exact USD cost of one call.

    Preserves the invariant that every billed token category is priced with
    its own multiplier. Cached reads are ~10% of list and cache writes carry a
    TTL-dependent premium; collapsing them into plain input tokens is the
    single easiest way to produce a cost model that looks right and is not.

    The result is exact, not rounded -- see :func:`quantize_usd`.
    """
    write_multiplier = (
        multipliers.cache_write_1h if cache_ttl == "1h" else multipliers.cache_write_5m
    )
    subtotal = (
        Decimal(usage.input_tokens) * price.input_per_mtok
        + Decimal(usage.output_tokens) * price.output_per_mtok
        + Decimal(usage.cache_read_input_tokens) * price.input_per_mtok * multipliers.cache_read
        + Decimal(usage.cache_creation_input_tokens) * price.input_per_mtok * write_multiplier
    ) / _PER_MTOK
    if batch:
        subtotal *= multipliers.batch
    return subtotal


@dataclass(frozen=True)
class PhaseEstimate:
    """Extrapolation from a sample of units to a whole phase (spec §12.4)."""

    phase: str
    sample_units: int
    sample_usd: Decimal
    total_units: int
    projected_usd: Decimal
    ceiling_usd: Decimal

    @property
    def within_ceiling(self) -> bool:
        return self.projected_usd <= self.ceiling_usd

    @property
    def per_unit_usd(self) -> Decimal:
        return self.sample_usd / Decimal(self.sample_units) if self.sample_units else Decimal(0)


def estimate_phase(
    *,
    phase: str,
    sample_units: int,
    sample_usd: Decimal,
    total_units: int,
    ceiling_usd: Decimal,
) -> PhaseEstimate:
    """Project a phase's spend from a measured sample.

    Preserves the guardrail that no full phase launches unmeasured: a 36,000
    run phase whose per-unit cost is 3x the model would otherwise be
    discovered by the invoice.
    """
    if sample_units <= 0:
        raise ValueError("cannot extrapolate from a zero-unit sample")
    projected = sample_usd / Decimal(sample_units) * Decimal(total_units)
    return PhaseEstimate(
        phase=phase,
        sample_units=sample_units,
        sample_usd=sample_usd,
        total_units=total_units,
        projected_usd=projected,
        ceiling_usd=ceiling_usd,
    )


@dataclass
class CostMeter:
    """Accumulates spend for one phase and enforces its ceiling.

    Construction takes the phase name so the ceiling comes from config rather
    than from the call site; passing ``ceiling_usd`` explicitly is for tests
    and the ``dev budget-probe`` acceptance hook.
    """

    settings: Settings
    phase: str
    ceiling_usd: Decimal | None = None
    total_usd: Decimal = field(default=Decimal(0), init=False)
    calls: int = field(default=0, init=False)
    cached_calls: int = field(default=0, init=False)
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=0, output_tokens=0), init=False)
    cached_usage: Usage = field(
        default_factory=lambda: Usage(input_tokens=0, output_tokens=0), init=False
    )
    # Services billed per call rather than per token, counted by kind and kept
    # out of `calls`. Folding a rerank query into `calls` would change the
    # denominator of `hit_rate`, and the M6 acceptance criterion (action-cache
    # hit rate >= 88%) is read straight off it -- a second kind of call
    # entering that ratio would move a measured criterion without touching the
    # thing it measures.
    units: dict[str, int] = field(default_factory=dict, init=False)
    cached_units: dict[str, int] = field(default_factory=dict, init=False)
    units_usd: Decimal = field(default=Decimal(0), init=False)
    _resume_state: dict[str, Any] = field(default_factory=dict, init=False)
    # What earlier processes already spent on this phase, read from its
    # checkpoint. `total_usd` stays this process's own spend, so per-run
    # reporting is unchanged; the ceiling is enforced on the sum.
    carried_usd: Decimal = field(default=Decimal(0), init=False)
    _recorded_usd: Decimal = field(default=Decimal(0), init=False)

    def __post_init__(self) -> None:
        if self.ceiling_usd is None:
            self.ceiling_usd = self.settings.phase_ceiling(self.phase)
        self.carried_usd = self._read_carried()
        self._recorded_usd = self.carried_usd

    def _read_carried(self) -> Decimal:
        """The phase's recorded spend, or zero when it has none.

        A checkpoint that exists and cannot be read is an error, not a zero:
        treating it as zero is exactly the re-granted ceiling this exists to
        prevent, and it would happen silently.
        """
        path = self.checkpoint_path()
        if not path.is_file():
            return Decimal(0)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            # The exact figure when present; checkpoints written before it
            # existed carry only the six-decimal one.
            return Decimal(str(payload.get("spent_usd_exact", payload["spent_usd"])))
        except (OSError, ValueError, KeyError, ArithmeticError) as exc:
            raise ValueError(
                f"cannot read the recorded spend for phase {self.phase!r} from {path}: "
                f"{type(exc).__name__}: {exc}. Refusing to start the phase at zero -- repair or "
                "delete the checkpoint deliberately."
            ) from exc

    @property
    def phase_usd(self) -> Decimal:
        """Everything spent on this phase, by this process and those before it."""
        return self.carried_usd + self.total_usd

    # -- resumability -------------------------------------------------------

    def set_resume_state(self, state: dict[str, Any]) -> None:
        """Record what a restart would need to skip completed units.

        Preserves invariant 8 (every phase is resumable). The meter holds this
        rather than the runner because the meter is what aborts, and a
        checkpoint written after the abort decision would race it.
        """
        self._resume_state = dict(state)

    def checkpoint_path(self) -> Path:
        return self.settings.checkpoint_path() / f"{self.phase}.checkpoint.json"

    def write_checkpoint(self) -> Path:
        """Persist the resume state atomically and return where it went."""
        path = self.checkpoint_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {
                "phase": self.phase,
                # Cumulative for the phase: this is what the next process
                # starts from.
                "spent_usd": str(quantize_usd(self.phase_usd)),
                "spent_usd_exact": str(self.phase_usd),
                "ceiling_usd": str(quantize_usd(self._ceiling())),
                "calls": self.calls,
                "cached_calls": self.cached_calls,
                "resume": self._resume_state,
            },
            indent=2,
            sort_keys=True,
        )
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{self.phase}-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            # Cleanup and re-raise. A checkpoint that half-exists is worse than
            # none: resume would read it and skip work that never ran.
            Path(temporary).unlink(missing_ok=True)
            raise
        return path

    # -- accounting ---------------------------------------------------------

    def _ceiling(self) -> Decimal:
        assert self.ceiling_usd is not None  # noqa: S101 -- set in __post_init__
        return self.ceiling_usd

    def price_of(self, model: str, usage: Usage, *, batch: bool) -> Decimal:
        """Price a call without booking it."""
        return compute_cost(
            usage=usage,
            price=self.settings.price_for(model),
            multipliers=self.settings.pricing_multipliers,
            batch=batch,
            cache_ttl=self.settings.prompt_cache.ttl,
        )

    def record(self, *, model: str, usage: Usage, batch: bool) -> Decimal:
        """Book one billed call, then enforce the ceiling.

        The ceiling is checked *after* the spend is added, so the reported
        figure is the true amount spent rather than the amount that would have
        been spent had the call been allowed. Returns the call's cost.
        """
        cost = self.price_of(model, usage, batch=batch)
        self.total_usd += cost
        self.calls += 1
        self.usage = self.usage + usage
        self._enforce()
        self._record_progress()
        return cost

    def _record_progress(self) -> None:
        """Checkpoint whenever spend has advanced by a percent of the ceiling.

        Bounds what a crash can lose to one percent of the ceiling without an
        fsync per call: at most a hundred writes per phase, against hundreds of
        thousands of calls.
        """
        step = self._ceiling() / Decimal(100)
        if step > 0 and self.phase_usd - self._recorded_usd >= step:
            self.write_checkpoint()
            self._recorded_usd = self.phase_usd

    def price_of_units(self, *, units: int, price_per_1k_usd: Decimal) -> Decimal:
        """Price per-call-billed work without booking it."""
        return compute_unit_cost(units=units, price_per_1k_usd=price_per_1k_usd)

    def record_units(self, *, kind: str, units: int, price_per_1k_usd: Decimal) -> Decimal:
        """Book ``units`` calls to a per-call-billed service, then enforce the ceiling.

        The same contract as :meth:`record`, one level away from tokens: the
        spend is added before the ceiling is checked, so the figure reported on
        a breach is what was actually spent rather than what would have been
        spent had the call been refused. Returns the cost.

        ``units`` must be positive. A booking of zero would record a call that
        was never made -- the caller with an empty pool does not reach a
        provider at all -- and it is the one shape that makes the per-query
        count disagree with the provider's invoice for a reason nothing
        downstream could see.
        """
        if units <= 0:
            raise ValueError(
                f"cannot book {units} {kind!r} unit(s): a call that billed nothing was not made"
            )
        cost = self.price_of_units(units=units, price_per_1k_usd=price_per_1k_usd)
        self.total_usd += cost
        self.units_usd += cost
        self.units[kind] = self.units.get(kind, 0) + units
        self._enforce()
        self._record_progress()
        return cost

    def record_unit_cache_hit(self, *, kind: str, units: int = 1) -> None:
        """Book per-call-billed work served from a recording, at zero.

        The mirror of :meth:`record_cache_hit` for a service that is not
        token-billed, and kept in its own counter for the same reason
        :attr:`units` is: the rerank cache and the action cache are different
        caches with different hit rates, and one ratio reported for both would
        describe neither.
        """
        if units <= 0:
            raise ValueError(
                f"cannot book {units} cached {kind!r} unit(s): a hit serves at least one call"
            )
        self.cached_units[kind] = self.cached_units.get(kind, 0) + units

    def record_cache_hit(self, *, model: str, usage: Usage) -> None:
        """Book a cache hit as a zero-cost call.

        Tracked rather than ignored so that hit rate and spend can be read off
        the same ledger (spec §12.2); the hit rate is the biggest single lever
        on total cost and an untracked lever cannot be tuned.
        """
        del model  # priced at zero by construction; kept for call-site symmetry
        self.cached_calls += 1
        self.cached_usage = self.cached_usage + usage

    def _enforce(self) -> None:
        if not self.settings.budget.abort_on_breach:
            return
        ceiling = self._ceiling()
        if self.phase_usd <= ceiling:
            return
        path = self.write_checkpoint()
        raise BudgetExceeded(
            phase=self.phase,
            spent=quantize_usd(self.phase_usd),
            ceiling=quantize_usd(ceiling),
            checkpoint=str(path),
        )

    # -- reporting ----------------------------------------------------------

    @property
    def hit_rate(self) -> float:
        """Fraction of calls served from the recording cache."""
        total = self.calls + self.cached_calls
        return self.cached_calls / total if total else 0.0

    def snapshot(self) -> dict[str, Any]:
        """A JSON-safe summary for the run ledger and the phase report."""
        return {
            "phase": self.phase,
            "calls": self.calls,
            "cached_calls": self.cached_calls,
            "hit_rate": self.hit_rate,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_input_tokens": self.usage.cache_read_input_tokens,
            "cache_creation_input_tokens": self.usage.cache_creation_input_tokens,
            # Sorted (invariant 7): the ledger is compared between runs, and a
            # key order that followed first use would diff with nothing behind
            # it.
            "units": {kind: self.units[kind] for kind in sorted(self.units)},
            "cached_units": {kind: self.cached_units[kind] for kind in sorted(self.cached_units)},
            "units_usd": str(quantize_usd(self.units_usd)),
            "total_usd": str(quantize_usd(self.total_usd)),
            "carried_usd": str(quantize_usd(self.carried_usd)),
            "phase_usd": str(quantize_usd(self.phase_usd)),
            "ceiling_usd": str(quantize_usd(self._ceiling())),
        }

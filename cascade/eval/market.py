"""The market's own probability at each cutoff (M14). Pure: no I/O, no clock.

171 of the 180 sealed scenarios are prediction-market questions, and the
registry reads each market's price only to learn which way it resolved. That
leaves the study without the one benchmark a forecasting reader asks for
first: *what did the market itself say at the cutoff?* A Brier score is a
number; "beats, or does not beat, the market on the questions both priced" is a
claim.

Three rules make the benchmark valid, and each is a function here rather than
a convention somewhere else:

* **The time lock is strict.** The price is the last observation with a
  timestamp *strictly before* ``cutoff_ts`` -- the same rule Chronofence
  applies to ``published_at``, where ``>= as_of`` is a violation. It is
  enforced twice: :func:`last_before` selects, and :class:`MarketPrice` refuses
  to be constructed around an observation at or after its cutoff, so a price
  that breaks the lock cannot cross a module boundary at all.
* **No price is ever imputed.** A market with no admissible observation yields
  a :class:`MarketPrice` carrying a reason and no probability. It is excluded
  from the baseline and counted, exactly as an unparseable baseline answer is
  "dropped and counted, never scored 0.5" (M7) -- 0.5 here would score a market
  that said nothing as a well-calibrated one.
* **A stale price is reported, not silently used.** Staleness is ``cutoff -
  observed_at``. A price older than ``market_baseline.max_staleness_hours`` is
  kept, shown with its age, and left out of the scored set; see
  ``configs/base.yaml`` for why the bound is what it is.

What the payload parsers read is as deliberate as what they return. Gamma's
market document carries ``outcomePrices`` -- the resolution -- and Manifold's
bets carry ``isFilled`` / ``isCancelled`` / ``fills``, which keep changing
after the cutoff. None of those is read: the Polymarket path takes the YES
token id and the creation time, the Manifold path takes ``createdTime``,
``probAfter`` and ``isRedemption``, all fixed at the moment they were written.

Whether an *agent* should ever see this price is a separate decision nobody
has made. Until it is, the price is a benchmark only, and migration 017
enforces that the way invariant 2 is enforced: ``cascade_sim`` holds no grant
on ``market_prices``. For the same reason the baseline is never written into
``forecasts``, which ``cascade_sim`` can read.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cascade.eval.schema import ScoredForecast
from cascade.retrieval.metrics import percentile

__all__ = [
    "MARKET_CONFIG_ID",
    "MARKET_SOURCES",
    "CoverageSummary",
    "MarketPayloadError",
    "MarketPrice",
    "MarketRef",
    "PriceObservation",
    "StalenessQuantiles",
    "UnobtainableReason",
    "coverage_summary",
    "is_stale",
    "last_before",
    "manifold_bets_url",
    "market_created_at",
    "parse_manifold_bets",
    "parse_polymarket_history",
    "parse_source_ref",
    "polymarket_history_url",
    "polymarket_market_url",
    "polymarket_yes_token",
    "scored_market_forecasts",
    "unobtainable",
]

MARKET_CONFIG_ID = "B4_market_at_cutoff"

# Sources that *have* a market. `curated` and `metaculus` questions are in the
# registry without one; they are counted as `not_a_market`, never priced.
MARKET_SOURCES: tuple[str, ...] = ("manifold", "polymarket")

GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
MANIFOLD_BETS_URL = "https://api.manifold.markets/v0/bets"

Unit = Annotated[float, Field(ge=0.0, le=1.0)]

UnobtainableReason = Literal[
    # The scenario's source has no market behind it (curated, Metaculus).
    "not_a_market",
    # `source_ref` does not name a market the way its loader writes it.
    "unparseable_ref",
    # The market document carries no YES/NO token pair to ask a price for.
    "no_yes_token",
    # The source says the market did not exist yet when the cutoff fell.
    "market_created_after_cutoff",
    # The source answered, and holds no observation in the lookback window.
    "no_price_history",
    # The source refused or failed. Nothing is recorded, so the next run
    # retries it; this is the one reason that is not a fact about the market.
    "fetch_failed",
    # Replay mode, and the response was never recorded.
    "not_recorded",
]


class MarketPayloadError(ValueError):
    """A source answered with a body that is not the shape its API documents."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_aware(value: datetime, field: str) -> datetime:
    """Reject a naive timestamp: it names no instant, so no lock can hold on it."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware; got a naive datetime")
    return value


class PriceObservation(_Frozen):
    """One thing a market said, and when it said it."""

    observed_at: datetime
    probability: Unit

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "observed_at")


class MarketRef(_Frozen):
    """A scenario's ``source_ref``, resolved to the id its source's API takes."""

    source: Literal["manifold", "polymarket"]
    market_id: str = Field(min_length=1)


class MarketPrice(_Frozen):
    """What is known about one scenario's market at its cutoff.

    Exactly one of two states, and the type admits no third: a probability with
    the timestamp it was observed at, or a reason there is none. There is no
    default probability, so nothing downstream can mistake "unobtainable" for a
    number -- and an observation at or after ``cutoff_ts`` fails validation, so
    the time lock holds at every boundary this type crosses.
    """

    scenario_id: str = Field(min_length=1)
    source: str
    source_ref: str
    cutoff_ts: datetime
    probability: Unit | None = None
    observed_at: datetime | None = None
    unobtainable_reason: UnobtainableReason | None = None
    detail: str = ""
    fetched_at: datetime

    @field_validator("cutoff_ts", "fetched_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "timestamp")

    @field_validator("observed_at")
    @classmethod
    def _optional_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value, "observed_at")

    @model_validator(mode="after")
    def _priced_or_explained(self) -> MarketPrice:
        priced = self.probability is not None
        if priced != (self.observed_at is not None):
            raise ValueError("probability and observed_at travel together or not at all")
        if priced == (self.unobtainable_reason is not None):
            raise ValueError(
                "a market price is either a probability or a reason there is none, never both "
                "and never neither"
            )
        if self.observed_at is not None and self.observed_at >= self.cutoff_ts:
            raise ValueError(
                f"observation at {self.observed_at.isoformat()} is not strictly before the "
                f"cutoff {self.cutoff_ts.isoformat()}: the time lock forbids it"
            )
        return self

    @property
    def priced(self) -> bool:
        return self.probability is not None

    @property
    def staleness(self) -> timedelta | None:
        """``cutoff - observed_at``; ``None`` when there is no observation."""
        return None if self.observed_at is None else self.cutoff_ts - self.observed_at


# ---------------------------------------------------------------------------
# The time lock
# ---------------------------------------------------------------------------


def last_before(
    observations: Iterable[PriceObservation], *, cutoff: datetime
) -> PriceObservation | None:
    """The last observation **strictly before** ``cutoff``, or ``None``.

    Preserves the time lock: an observation stamped exactly at the cutoff is
    not admissible, matching Chronofence, where ``published_at >= as_of`` is a
    violation. ``cutoff`` has no default (invariant 1) -- a caller that forgets
    it gets a ``TypeError``, not the newest price the source holds, which for a
    resolved market is the outcome.

    Among observations sharing a timestamp the one latest in the given order
    wins, so callers pass chronological order and the choice is a function of
    the data rather than of a sort's tie-breaking.
    """
    _require_aware(cutoff, "cutoff")
    best: tuple[datetime, int] | None = None
    chosen: PriceObservation | None = None
    for index, observation in enumerate(observations):
        if observation.observed_at >= cutoff:
            continue
        key = (observation.observed_at, index)
        if best is None or key > best:
            best, chosen = key, observation
    return chosen


def is_stale(price: MarketPrice, *, max_staleness: timedelta) -> bool:
    """True when a priced scenario's observation is older than the bound.

    The bound is inclusive on the usable side: an observation exactly
    ``max_staleness`` old is still usable, one tick older is not. An unpriced
    scenario is never "stale" -- it has its own reason, and folding the two
    together would hide how many markets simply had nothing to say.
    """
    age = price.staleness
    return age is not None and age > max_staleness


# ---------------------------------------------------------------------------
# Source references and URLs
# ---------------------------------------------------------------------------


def parse_source_ref(source: str, source_ref: str) -> MarketRef | None:
    """Resolve a registry ``source_ref`` to the id its API is asked with.

    The shapes are the ones the loaders write: ``polymarket:{slug}:{market_id}``
    (``cascade/ledger/sources/polymarket.py``) and ``manifold:{contract_id}``.
    ``None`` for anything else, rather than a guess at which segment is the id.
    """
    parts = source_ref.split(":")
    if source == "polymarket" and len(parts) >= 3 and parts[0] == "polymarket":
        market_id = parts[-1].strip()
        return MarketRef(source="polymarket", market_id=market_id) if market_id.isdigit() else None
    if source == "manifold" and len(parts) == 2 and parts[0] == "manifold" and parts[1].strip():
        return MarketRef(source="manifold", market_id=parts[1].strip())
    return None


def polymarket_market_url(market_id: str) -> str:
    """Gamma's document for one market, which carries its CLOB token ids."""
    return f"{GAMMA_MARKET_URL}/{market_id}"


def polymarket_history_url(
    token_id: str, *, start: datetime, cutoff: datetime, fidelity_minutes: int
) -> str:
    """CLOB price history for one token over ``[start, cutoff]``.

    The window *ends at the cutoff*, so the request never asks for a
    post-cutoff price -- for a resolved market those converge on the outcome.
    The end is rounded **up** to a whole second: rounding down would drop an
    admissible observation in the cutoff's final fractional second, and the
    one extra second this can admit is what :func:`last_before` exists to
    refuse. The URL is a request; the lock is the selector.
    """
    _require_aware(start, "start")
    _require_aware(cutoff, "cutoff")
    return (
        f"{CLOB_HISTORY_URL}?market={token_id}"
        f"&startTs={math.floor(start.timestamp())}&endTs={math.ceil(cutoff.timestamp())}"
        f"&fidelity={fidelity_minutes}"
    )


def manifold_bets_url(contract_id: str, *, cutoff: datetime, limit: int) -> str:
    """The newest ``limit`` bets on one contract created before the cutoff.

    Rounded up to a whole millisecond for the same reason as
    :func:`polymarket_history_url`; :func:`last_before` holds the lock.
    """
    _require_aware(cutoff, "cutoff")
    return (
        f"{MANIFOLD_BETS_URL}?contractId={contract_id}"
        f"&beforeTime={math.ceil(cutoff.timestamp() * 1000)}&limit={limit}"
    )


# ---------------------------------------------------------------------------
# Payload parsers -- each reads only fields fixed at the time they were written
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    """Gamma returns ``outcomes`` / ``clobTokenIds`` as JSON-encoded strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def polymarket_yes_token(market: Mapping[str, Any]) -> str | None:
    """The CLOB token id of the YES leg, or ``None`` when there is no such leg.

    Reads ``outcomes`` and ``clobTokenIds`` and nothing else. In particular it
    never reads ``outcomePrices``, which on a resolved market *is* the label:
    the benchmark must be obtainable by a process that has never seen one.

    Only an exact ``["Yes", "No"]`` pair is accepted, the same shape the
    registry admitted the market under, so index 0 is YES by the source's own
    ordering rather than by assumption.
    """
    outcomes = [str(item).strip().lower() for item in _as_list(market.get("outcomes"))]
    tokens = [str(item).strip() for item in _as_list(market.get("clobTokenIds"))]
    if outcomes != ["yes", "no"] or len(tokens) != 2 or not tokens[0]:
        return None
    return tokens[0]


def market_created_at(market: Mapping[str, Any]) -> datetime | None:
    """When Gamma says the market was created, as aware UTC, or ``None``.

    Used only to *explain* an empty history: a market created after the cutoff
    had no price at the cutoff, which is a fact about the registry's cutoff and
    worth telling apart from a market that existed and never traded.
    """
    value = market.get("createdAt")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed.astimezone(UTC)


def _probability(value: Any) -> float | None:
    """A float in [0, 1], or ``None``. ``bool`` is excluded: ``True`` is not 1.0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


def _epoch(value: Any, *, per_second: int) -> datetime | None:
    """An aware UTC instant from an epoch count, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value) / per_second, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_polymarket_history(payload: Any) -> tuple[PriceObservation, ...]:
    """Observations from a CLOB ``/prices-history`` body, in chronological order.

    The documented shape is ``{"history": [{"t": <unix seconds>, "p": <price>}]}``.
    A body without a ``history`` list is an error, not an empty market: treating
    a changed API as "this market never traded" would turn a broken fetcher
    into a coverage statistic. A single point that cannot be read is skipped --
    a loader never invents a field (``cascade/ledger/sources``).
    """
    history = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(history, list):
        raise MarketPayloadError("prices-history body carries no `history` list")
    out: list[PriceObservation] = []
    for point in history:
        if not isinstance(point, dict):
            continue
        observed_at = _epoch(point.get("t"), per_second=1)
        probability = _probability(point.get("p"))
        if observed_at is None or probability is None:
            continue
        out.append(PriceObservation(observed_at=observed_at, probability=probability))
    # Stable: equal timestamps keep the source's order, which is what
    # `last_before` breaks ties on.
    return tuple(sorted(out, key=lambda item: item.observed_at))


def parse_manifold_bets(payload: Any) -> tuple[PriceObservation, ...]:
    """Observations from a Manifold ``/v0/bets`` body, in chronological order.

    A binary contract's probability is the AMM's state, and ``probAfter`` is
    that state immediately after the bet at ``createdTime``. Both are written
    once. ``isFilled``, ``isCancelled``, ``amount`` and ``fills`` are not --
    a limit order keeps filling after the cutoff -- so none of them is read.

    Redemptions are skipped: they are bookkeeping entries created as a side
    effect of another bet, they move no price, and counting one as an
    observation would make a quiet market look freshly quoted.

    The API answers newest first; the result is reversed to chronological so
    :func:`last_before` breaks a same-millisecond tie toward the later bet.
    """
    if not isinstance(payload, list):
        raise MarketPayloadError("bets body is not a list")
    out: list[PriceObservation] = []
    for bet in reversed(payload):
        if not isinstance(bet, dict) or bet.get("isRedemption") is True:
            continue
        observed_at = _epoch(bet.get("createdTime"), per_second=1000)
        probability = _probability(bet.get("probAfter"))
        if observed_at is None or probability is None:
            continue
        out.append(PriceObservation(observed_at=observed_at, probability=probability))
    return tuple(sorted(out, key=lambda item: item.observed_at))


def unobtainable(
    *,
    scenario_id: str,
    source: str,
    source_ref: str,
    cutoff: datetime,
    reason: UnobtainableReason,
    fetched_at: datetime,
    detail: str = "",
) -> MarketPrice:
    """A :class:`MarketPrice` that says why there is no price. Never imputes one."""
    return MarketPrice(
        scenario_id=scenario_id,
        source=source,
        source_ref=source_ref,
        cutoff_ts=cutoff,
        unobtainable_reason=reason,
        detail=detail,
        fetched_at=fetched_at,
    )


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


class StalenessQuantiles(_Frozen):
    """How old the priced observations were at their cutoffs, in seconds."""

    n: int
    minimum: float
    p50: float
    p90: float
    p99: float
    maximum: float


class CoverageSummary(_Frozen):
    """What the benchmark covers, and what it does not, with counts.

    ``n_usable + n_stale + sum(unobtainable) == n_scenarios`` always; a summary
    whose parts do not add up to the registry is one that lost a scenario.
    """

    n_scenarios: int
    n_priced: int
    n_usable: int
    n_stale: int
    max_staleness_seconds: float
    unobtainable: tuple[tuple[str, int], ...]
    by_source: tuple[tuple[str, int, int, int], ...]
    """``(source, scenarios, usable, stale)``, sorted by source."""
    staleness: StalenessQuantiles | None
    """Over every *priced* scenario, stale ones included -- the distribution is
    what lets a reader judge the bound, so the bound must not pre-filter it."""


def _quantiles(seconds: Sequence[float]) -> StalenessQuantiles | None:
    if not seconds:
        return None
    return StalenessQuantiles(
        n=len(seconds),
        minimum=min(seconds),
        p50=percentile(seconds, 50.0),
        p90=percentile(seconds, 90.0),
        p99=percentile(seconds, 99.0),
        maximum=max(seconds),
    )


def coverage_summary(prices: Sequence[MarketPrice], *, max_staleness: timedelta) -> CoverageSummary:
    """Count what was priced, what was stale, and what was unobtainable and why.

    Every scenario lands in exactly one of usable / stale / one reason, so the
    excluded are *counted* rather than merely absent -- a benchmark reported
    over 150 scenarios as though it were 180 is the drift nothing downstream
    can detect (``cascade/eval/store.py``).
    """
    ids = [price.scenario_id for price in prices]
    if len(set(ids)) != len(ids):
        raise ValueError("coverage is per scenario; a scenario id appears more than once")

    reasons: dict[str, int] = {}
    sources: dict[str, list[int]] = {}
    ages: list[float] = []
    usable = stale = 0
    for price in sorted(prices, key=lambda item: item.scenario_id):
        row = sources.setdefault(price.source, [0, 0, 0])
        row[0] += 1
        age = price.staleness
        if age is None:
            # MarketPrice's validator guarantees a reason wherever there is no
            # observation, so this is every unpriced scenario and only those.
            reason = str(price.unobtainable_reason)
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        ages.append(age.total_seconds())
        if is_stale(price, max_staleness=max_staleness):
            stale += 1
            row[2] += 1
        else:
            usable += 1
            row[1] += 1

    return CoverageSummary(
        n_scenarios=len(prices),
        n_priced=usable + stale,
        n_usable=usable,
        n_stale=stale,
        max_staleness_seconds=max_staleness.total_seconds(),
        unobtainable=tuple(sorted(reasons.items())),
        by_source=tuple(
            (source, counts[0], counts[1], counts[2]) for source, counts in sorted(sources.items())
        ),
        staleness=_quantiles(ages),
    )


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------


def scored_market_forecasts(
    rows: Sequence[tuple[MarketPrice, Literal[0, 1], str]], *, max_staleness: timedelta
) -> tuple[ScoredForecast, ...]:
    """Turn ``(price, outcome, domain)`` rows into the baseline's scored set.

    Only a usable price is scored: unobtainable and stale scenarios are left
    out, never filled with 0.5 or the base rate, so every comparison against
    this baseline runs on the intersection and carries its own count.

    The probability is passed through **unclipped**. A market at 0.999 is a
    real forecast and its Brier is computed on it as quoted; log loss is
    already clipped once, for every configuration alike, in
    ``cascade/eval/metrics.py`` -- a second rule here would score the market
    differently from the system it is compared with.

    ``policy`` is ``"none"``: no decider of this study's produced the number.
    """
    out: list[ScoredForecast] = []
    for price, outcome, domain in sorted(rows, key=lambda row: row[0].scenario_id):
        if price.probability is None or is_stale(price, max_staleness=max_staleness):
            continue
        out.append(
            ScoredForecast(
                scenario_id=price.scenario_id,
                config_id=MARKET_CONFIG_ID,
                p_hat=price.probability,
                outcome=outcome,
                domain=domain,
                policy="none",
            )
        )
    return tuple(out)

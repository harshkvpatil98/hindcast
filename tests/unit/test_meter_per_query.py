"""Per-call billing in the cost meter: exact, ceilinged, and kept apart (ADR-0047).

A managed reranker bills per query rather than per token, so it cannot ride on
the token price table -- the only honest token count for it is zero, which
prices every query at nothing. These tests hold the per-call path to the same
three properties the token path has, and to one more that is specific to it:

* the arithmetic is exact ``Decimal``, never binary float;
* the spend enters the phase ceiling, and a breach aborts with a checkpoint;
* a per-call booking never touches ``calls``, because ``hit_rate`` is computed
  over ``calls`` and the M6 acceptance criterion (action-cache hit rate >= 88%)
  is read straight off it.

Every figure below is hand-checkable from the rate and the count.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cascade.config import Settings
from cascade.llm.meter import CostMeter, compute_unit_cost, quantize_usd
from cascade.llm.types import BudgetExceeded, Usage

HAIKU = "claude-haiku-4-5-20251001"

# $2.00 per 1,000 queries is $0.002 per query. Deliberately not a round number
# of cents, so a rate divided in the wrong place is a visibly wrong figure.
TWO_DOLLARS_PER_1K = Decimal("2.00")


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("units", "rate", "expected"),
    [
        (1, "2.00", "0.002"),
        (7, "2.00", "0.014"),
        (1000, "2.00", "2.000"),
        # A rate that is not a binary fraction: $0.07 per 1,000 is $0.00007.
        (3, "0.07", "0.00021"),
        (0, "2.00", "0"),
    ],
)
def test_a_per_call_price_is_the_rate_over_a_thousand(units: int, rate: str, expected: str) -> None:
    """Exactly ``units * rate / 1000``, with no rounding on the way."""
    assert compute_unit_cost(units=units, price_per_1k_usd=Decimal(rate)) == Decimal(expected)


def test_accumulating_per_query_spend_does_not_drift(settings: Settings) -> None:
    """Measured: 10,000 queries at $0.07/1k is $0.70000 exactly, and 0.7000000000000705 in float.

    The drift is the reason this path is ``Decimal`` rather than the obvious
    float division. It is invisible in one booking and it is what would put
    the M8 ledger reconciliation outside its 2% band against a correct meter.
    """
    meter = CostMeter(settings, "bench", ceiling_usd=Decimal("1000"))
    for _ in range(10_000):
        meter.record_units(kind="rerank", units=1, price_per_1k_usd=Decimal("0.07"))

    assert meter.total_usd == Decimal("0.70000")
    assert meter.units == {"rerank": 10_000}

    drifted = 0.0
    for _ in range(10_000):
        drifted += 0.07 / 1000
    assert drifted != float(meter.total_usd), "float accumulation is the thing being avoided"


def test_a_negative_count_or_rate_is_refused() -> None:
    """Neither is a call that could have happened; both would credit the phase."""
    with pytest.raises(ValueError, match="not a call"):
        compute_unit_cost(units=-1, price_per_1k_usd=TWO_DOLLARS_PER_1K)
    with pytest.raises(ValueError, match="never negative"):
        compute_unit_cost(units=1, price_per_1k_usd=Decimal("-0.01"))


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------


def test_booking_a_query_returns_its_cost_and_adds_it_to_the_phase(settings: Settings) -> None:
    meter = CostMeter(settings, "bench", ceiling_usd=Decimal("1000"))
    cost = meter.record_units(kind="rerank", units=1, price_per_1k_usd=TWO_DOLLARS_PER_1K)

    assert cost == Decimal("0.002")
    assert meter.total_usd == Decimal("0.002")
    assert meter.units_usd == Decimal("0.002")
    assert meter.phase_usd == Decimal("0.002")


def test_a_zero_unit_booking_is_refused(settings: Settings) -> None:
    """A call that billed nothing was not made, and would not appear on the invoice."""
    meter = CostMeter(settings, "bench", ceiling_usd=Decimal("1000"))
    with pytest.raises(ValueError, match="was not made"):
        meter.record_units(kind="rerank", units=0, price_per_1k_usd=TWO_DOLLARS_PER_1K)
    with pytest.raises(ValueError, match="at least one call"):
        meter.record_unit_cache_hit(kind="rerank", units=0)
    assert meter.units == {}
    assert meter.cached_units == {}


def test_kinds_are_counted_separately(settings: Settings) -> None:
    """Two per-call services are two lines on the ledger, not one sum."""
    meter = CostMeter(settings, "bench", ceiling_usd=Decimal("1000"))
    meter.record_units(kind="rerank", units=2, price_per_1k_usd=TWO_DOLLARS_PER_1K)
    meter.record_units(kind="guardrail", units=1, price_per_1k_usd=Decimal("1.00"))

    assert meter.units == {"guardrail": 1, "rerank": 2}
    assert meter.total_usd == Decimal("0.005")  # 2 x 0.002 + 1 x 0.001


def test_a_cached_unit_books_nothing_and_is_still_counted(settings: Settings) -> None:
    """A replayed study must not report zero rerank activity.

    The count is what says whether the recorded corpus covers the pools this
    run asked for; the cost is zero because nothing was called.
    """
    meter = CostMeter(settings, "bench", ceiling_usd=Decimal("1000"))
    meter.record_unit_cache_hit(kind="rerank")
    meter.record_unit_cache_hit(kind="rerank")

    assert meter.cached_units == {"rerank": 2}
    assert meter.units == {}
    assert meter.total_usd == Decimal(0)


# ---------------------------------------------------------------------------
# The two ratios stay separate (the M6 criterion)
# ---------------------------------------------------------------------------


def test_per_call_work_never_enters_the_model_call_hit_rate(settings: Settings) -> None:
    """One model call, one hit, twenty rerank queries: the hit rate is still 0.5.

    Folding rerank queries into ``calls`` would move the M6 acceptance
    criterion without changing a single thing it measures -- a criterion
    passing for arithmetic reasons is the failure §1 is written against.
    """
    meter = CostMeter(settings, "bench", ceiling_usd=Decimal("1000"))
    usage = Usage(input_tokens=10, output_tokens=5)
    meter.record(model=HAIKU, usage=usage, batch=False)
    meter.record_cache_hit(model=HAIKU, usage=usage)
    for _ in range(10):
        meter.record_units(kind="rerank", units=1, price_per_1k_usd=TWO_DOLLARS_PER_1K)
        meter.record_unit_cache_hit(kind="rerank")

    assert meter.calls == 1
    assert meter.cached_calls == 1
    assert meter.hit_rate == 0.5
    assert meter.units == {"rerank": 10}
    assert meter.cached_units == {"rerank": 10}


def test_per_call_spend_is_reported_beside_token_spend_not_instead_of_it(
    settings: Settings,
) -> None:
    """``total_usd`` is the phase's money; ``units_usd`` says how much was not tokens."""
    meter = CostMeter(settings, "bench", ceiling_usd=Decimal("1000"))
    token_cost = meter.record(
        model=HAIKU, usage=Usage(input_tokens=1000, output_tokens=0), batch=False
    )
    unit_cost = meter.record_units(kind="rerank", units=1, price_per_1k_usd=TWO_DOLLARS_PER_1K)

    assert meter.units_usd == unit_cost
    assert meter.total_usd == token_cost + unit_cost


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------


def test_a_per_query_breach_aborts_and_checkpoints(settings: Settings) -> None:
    """Per-call spend buys from the same ceiling, and a breach is exit code 2.

    The ceiling is checked after the spend is added, so the reported figure is
    what was spent rather than what would have been spent had the query been
    refused -- the money left the account either way.
    """
    meter = CostMeter(settings, "simulate", ceiling_usd=Decimal("0.01"))
    meter.set_resume_state({"completed_units": 3, "next_unit": 4})

    with pytest.raises(BudgetExceeded) as caught:
        meter.record_units(kind="rerank", units=10, price_per_1k_usd=Decimal("10.00"))

    breach = caught.value
    assert breach.phase == "simulate"
    assert breach.spent == quantize_usd(Decimal("0.1"))  # 10 x $0.01
    assert breach.ceiling == Decimal("0.010000")

    payload = json.loads(Path(breach.checkpoint).read_text(encoding="utf-8"))
    assert payload["resume"] == {"completed_units": 3, "next_unit": 4}
    assert Decimal(payload["spent_usd_exact"]) == Decimal("0.1")


def test_a_resumed_phase_starts_from_the_per_query_spend_it_already_booked(
    settings: Settings,
) -> None:
    """The ceiling belongs to the phase, not the process -- M12's finding, per query.

    A retry loop around a rerank-bearing phase must not be granted the whole
    ceiling again.
    """
    first = CostMeter(settings, "bench", ceiling_usd=Decimal("0.01"))
    first.record_units(kind="rerank", units=4, price_per_1k_usd=Decimal("1.00"))  # $0.004
    first.write_checkpoint()

    second = CostMeter(settings, "bench", ceiling_usd=Decimal("0.01"))
    assert second.carried_usd == Decimal("0.004")
    with pytest.raises(BudgetExceeded):
        second.record_units(kind="rerank", units=7, price_per_1k_usd=Decimal("1.00"))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_the_snapshot_carries_the_per_call_counts_sorted(settings: Settings) -> None:
    """Sorted by kind (invariant 7), and JSON-safe: the ledger is diffed between runs."""
    meter = CostMeter(settings, "bench", ceiling_usd=Decimal("1000"))
    for kind in ("rerank", "guardrail", "aardvark"):
        meter.record_units(kind=kind, units=1, price_per_1k_usd=TWO_DOLLARS_PER_1K)
        meter.record_unit_cache_hit(kind=kind)

    round_tripped = json.loads(json.dumps(meter.snapshot()))
    assert list(round_tripped["units"]) == ["aardvark", "guardrail", "rerank"]
    assert list(round_tripped["cached_units"]) == ["aardvark", "guardrail", "rerank"]
    assert Decimal(round_tripped["units_usd"]) == Decimal("0.006")


def test_pricing_a_unit_does_not_book_it(settings: Settings) -> None:
    """`price_of_units` is the estimator's door, and an estimate is not a spend."""
    meter = CostMeter(settings, "bench", ceiling_usd=Decimal("1000"))
    assert meter.price_of_units(units=5, price_per_1k_usd=TWO_DOLLARS_PER_1K) == Decimal("0.010")
    assert meter.total_usd == Decimal(0)
    assert meter.units == {}

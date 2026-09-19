"""Accuracy by evidence quality. Pure: no I/O, no clock, no RNG.

The question, asked by the study's owner before any forecast existed: *do
scenarios with richer evidence forecast better?* This module is the analysis,
declared in advance so that its answer cannot be shaped by its result.

**The measure.** For each scenario, the number of corpus chunks published in
the final :data:`EVIDENCE_WINDOW_DAYS` days before its cutoff --
``cutoff - 30d <= published_at < cutoff``. Counted by
``cascade.eval.store.evidence_counts``; this module never touches the corpus.
The final month rather than everything admissible, because a late-cutoff
scenario can see millions of chunks that are years stale, and "how much could
an agent have read about *this* question's moment" is the quantity the claim
is about.

**The tiers are fixed chunk thresholds, not quantiles.** Quantile tiers are
redrawn by the data they describe: as the corpus rebuild finishes, the same
scenario would drift between "thin" and "moderate" without its evidence
changing at all, and tier boundaries chosen after forecasts exist are one more
thing that could be chosen to make a contrast appear. The boundaries below
were read off the distribution measured on 2026-09-19, before any forecast:
of the 180 sealed scenarios, 7 had no chunk in the window, 75 had 1-999,
15 had 1,000-9,999 and 83 had 10,000 or more. Those counts will move as the
corpus grows; the thresholds will not.

**What this analysis cannot show.** Evidence volume is not assigned at random.
It rises with the cutoff year (CC-NEWS coverage is deepest for recent months)
and differs by domain, and both of those also move forecast difficulty. A
gradient across tiers is a description of where the system does well, not an
estimate of what more evidence would do. The report says so beside the table.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from cascade.eval.metrics import brier
from cascade.eval.schema import EvidenceFinding, EvidenceTierMetrics, ScoredForecast
from cascade.eval.stats import permutation_p, spearman

__all__ = [
    "EVIDENCE_WINDOW_DAYS",
    "TIERS",
    "EvidenceTier",
    "evidence_finding",
    "per_tier",
    "tier_of",
]

EVIDENCE_WINDOW_DAYS = 30


@dataclass(frozen=True, slots=True)
class EvidenceTier:
    """One declared tier: a name and an inclusive chunk-count range."""

    name: str
    lo: int
    hi: int | None
    """Inclusive upper bound, or ``None`` for the open top tier."""

    def holds(self, count: int) -> bool:
        return count >= self.lo and (self.hi is None or count <= self.hi)


# Declared constants, not configuration. A threshold that an environment
# variable can move is a threshold that can be moved after looking, and the
# override would leave no trace in the repository. Changing a tier is a commit.
TIERS: tuple[EvidenceTier, ...] = (
    EvidenceTier("none", 0, 0),
    EvidenceTier("thin", 1, 999),
    EvidenceTier("moderate", 1_000, 9_999),
    EvidenceTier("rich", 10_000, None),
)


def tier_of(count: int) -> EvidenceTier:
    """The one tier a chunk count belongs to.

    Preserves a partition of the non-negative integers: every count falls in
    exactly one tier, so no scenario is dropped from the table and none is
    counted twice. A negative count is a broken query, not thin evidence.
    """
    if count < 0:
        raise ValueError(f"a chunk count cannot be negative, got {count}")
    matches = [tier for tier in TIERS if tier.holds(count)]
    if len(matches) != 1:
        raise ValueError(f"tiers do not partition the counts: {count} matched {len(matches)}")
    return matches[0]


def _counts_for(scored: Sequence[ScoredForecast], counts: Mapping[str, int]) -> list[int]:
    """Each scored scenario's measured count, or a refusal naming the gaps.

    A scenario with no measured count is not a scenario with no evidence.
    Defaulting it to zero would file every unmeasured scenario under "none" --
    the tier the claim under test is most sensitive to.
    """
    missing = sorted(item.scenario_id for item in scored if item.scenario_id not in counts)
    if missing:
        raise ValueError(
            f"{len(missing)} scored scenario(s) have no measured evidence count "
            f"(first: {missing[0]!r}); refusing to tier them as zero"
        )
    return [counts[item.scenario_id] for item in scored]


def per_tier(
    scored: Sequence[ScoredForecast], counts: Mapping[str, int]
) -> tuple[EvidenceTierMetrics, ...]:
    """Brier by evidence tier, with counts. One row per declared tier, always.

    Preserves the tier definition's independence from the data: rows are the
    declared :data:`TIERS` in declared order, whatever fell into them. The
    Brier is ``metrics.brier`` over the tier's scenarios, so a tier row and
    the headline are the same arithmetic on different subsets.
    """
    ordered = sorted(scored, key=lambda item: item.scenario_id)
    measured = _counts_for(ordered, counts)
    rows: list[EvidenceTierMetrics] = []
    for tier in TIERS:
        members = [item for item, count in zip(ordered, measured, strict=True) if tier.holds(count)]
        rows.append(
            EvidenceTierMetrics(
                tier=tier.name,
                lo=tier.lo,
                hi=tier.hi,
                n=len(members),
                base_rate=(
                    sum(item.outcome for item in members) / len(members) if members else None
                ),
                brier=(
                    brier([item.p_hat for item in members], [item.outcome for item in members])
                    if members
                    else None
                ),
            )
        )
    return tuple(rows)


def evidence_finding(
    scored: Sequence[ScoredForecast],
    counts: Mapping[str, int],
    *,
    seed: int,
    permutations: int = 10_000,
) -> EvidenceFinding:
    """The tier table plus the one pre-declared test of the claim.

    Preserves a single answer to a single question. The test is Spearman's rho
    between the raw chunk count and the per-scenario squared error, with the
    same seeded permutation p-value the dispersion finding uses -- on counts,
    not tiers, so the tier boundaries cannot influence it. ``None`` when it is
    undefined (fewer than two scenarios, or every count equal), never zero.
    """
    ordered = sorted(scored, key=lambda item: item.scenario_id)
    measured = [float(count) for count in _counts_for(ordered, counts)]
    errors = [(item.p_hat - item.outcome) ** 2 for item in ordered]
    rho = spearman(measured, errors)
    return EvidenceFinding(
        window_days=EVIDENCE_WINDOW_DAYS,
        n=len(ordered),
        tiers=per_tier(ordered, counts),
        spearman_rho=rho,
        spearman_p=(
            permutation_p(measured, errors, seed=seed, permutations=permutations, rank_based=True)
            if rho is not None
            else None
        ),
    )

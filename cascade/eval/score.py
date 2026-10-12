"""Turning scored forecasts into §10's metrics. Pure: no I/O, no clock.

Takes :class:`ScoredForecast` tuples -- the only labelled type in Assay -- and
returns the metric objects the report prints. The split between this module and
``store.py`` is the same one that makes ``metrics.py`` property-testable: the
database read is a thin shell, and everything that decides what a number *is*
happens here, where a test can hand it a list.

The recalibration split is derived from the study salt, so the "held-out half"
of §10.5 is the same half on every run of the report. A half chosen by a fresh
shuffle would make the post-calibration Brier move between two runs of the same
report, and the number most likely to be quoted out of context is the one that
must not drift.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from typing import Literal

from cascade.eval.ablation import ComparisonSpec, comparison_family
from cascade.eval.metrics import (
    CALIBRATION_BINS,
    auc,
    brier,
    brier_skill_score,
    calibration,
    isotonic_fit,
    isotonic_predict,
    log_loss,
    murphy_decomposition,
    per_domain,
)
from cascade.eval.schema import (
    CalibrationReport,
    Comparison,
    ComparisonFamily,
    DispersionFinding,
    DomainMetrics,
    MetricSet,
    ScoredForecast,
)
from cascade.eval.split import Partition, SplitDeclaration, require_dev_only, select
from cascade.eval.stats import (
    adjust_family,
    bootstrap_seed,
    paired_bootstrap,
    pearson,
    permutation_p,
    spearman,
)
from cascade.eval.supplementary import supplementary_family, supplementary_ids

__all__ = [
    "calibration_of",
    "climatology_reference",
    "compare_family",
    "dispersion_finding",
    "domains_of",
    "headline_by_partition",
    "measure",
    "metrics_for",
    "paired_reference",
    "recalibrated_brier",
    "recalibration_half",
    "significance_families",
    "split_halves",
]


def _columns(scored: Sequence[ScoredForecast]) -> tuple[list[float], list[int]]:
    return [item.p_hat for item in scored], [item.outcome for item in scored]


def recalibration_half(scenario_id: str, *, salt: str) -> Literal["fit", "held"]:
    """Which recalibration half one scenario falls in. A function of its id.

    Preserves per-id assignment, which is what lets the dev/test split compose
    with this one: a scenario's half does not depend on which other scenarios
    are present, so handing :func:`split_halves` the test partition alone
    splits *the test partition* -- the fit cannot reach across into dev -- and
    ``split.interactions`` can count the halves from ids without a label in
    sight.
    """
    digest = hashlib.blake2b(
        scenario_id.encode("utf-8"), digest_size=8, key=salt.encode("utf-8")
    ).digest()
    return "fit" if digest[0] & 1 else "held"


def split_halves(
    scored: Sequence[ScoredForecast], *, salt: str
) -> tuple[tuple[ScoredForecast, ...], tuple[ScoredForecast, ...]]:
    """Deterministic two-way split of the scenario set, keyed by the study salt.

    Assignment is a keyed hash of the scenario id, so it is stable across
    runs, independent of the order forecasts were loaded in, and -- critically
    -- **independent of the outcome**. Splitting on anything correlated with
    the label would make the held-out half a different population from the
    fitting half, and the recalibrated Brier would measure that instead of
    calibration.

    The halves are not guaranteed to be exactly equal in size; a hash is not a
    partition of fixed cardinality. Both counts travel with the result.
    """
    fit: list[ScoredForecast] = []
    held: list[ScoredForecast] = []
    for item in sorted(scored, key=lambda value: value.scenario_id):
        (fit if recalibration_half(item.scenario_id, salt=salt) == "fit" else held).append(item)
    return tuple(fit), tuple(held)


def recalibrated_brier(scored: Sequence[ScoredForecast], *, salt: str) -> float | None:
    """§10.5's isotonic recalibration, fitted on one half, scored on the other.

    "Never report the recalibrated figure as the headline -- the headline is
    the raw system." It is returned as a separate, optional field for exactly
    that reason, and it is ``None`` when either half is too small to fit or
    score, rather than a number computed from four scenarios.
    """
    fit, held = split_halves(scored, salt=salt)
    if len(fit) < 2 or len(held) < 2:
        return None
    fit_p, fit_y = _columns(fit)
    held_p, held_y = _columns(held)
    thresholds, values = isotonic_fit(fit_p, fit_y)
    mapped = isotonic_predict(thresholds, values, held_p)
    return brier(mapped, held_y)


def calibration_of(
    scored: Sequence[ScoredForecast], *, bins: int = CALIBRATION_BINS
) -> CalibrationReport:
    """§10.5's reliability table for one configuration."""
    forecasts, outcomes = _columns(scored)
    return calibration(forecasts, outcomes, bins=bins)


def domains_of(scored: Sequence[ScoredForecast]) -> tuple[DomainMetrics, ...]:
    """§10.4's per-domain breakdown for one configuration."""
    forecasts, outcomes = _columns(scored)
    return per_domain(forecasts, outcomes, [item.domain for item in scored])


def metrics_for(
    scored: Sequence[ScoredForecast],
    *,
    config_id: str,
    climatology_brier: float | None = None,
    direct_brier: float | None = None,
    salt: str | None = None,
    bins: int = CALIBRATION_BINS,
) -> MetricSet:
    """Every §10.1 metric for one configuration, measured on ``scored``.

    ``climatology_brier`` comes from the sealed manifest rather than being
    recomputed here: §10.2 calls climatology the floor, and a floor recomputed
    against whatever set happens to be loaded moves in the direction that
    flatters the result.

    The skill scores are ``None`` when their reference is absent. A missing
    baseline is a missing number; substituting a plausible one is precisely
    what §1 forbids.
    """
    if not scored:
        raise ValueError(
            f"no scored forecasts for {config_id!r}; a metric set over an empty "
            "set would be a row of numbers with nothing behind it"
        )
    forecasts, outcomes = _columns(scored)
    score = brier(forecasts, outcomes)
    report = calibration(forecasts, outcomes, bins=bins)
    return MetricSet(
        config_id=config_id,
        n=len(scored),
        base_rate=sum(outcomes) / len(outcomes),
        mean_p_hat=sum(forecasts) / len(forecasts),
        brier=score,
        log_loss=log_loss(forecasts, outcomes),
        auc=auc(forecasts, outcomes),
        ece=report.ece,
        mce=report.mce,
        murphy=murphy_decomposition(forecasts, outcomes, bins=bins),
        policies=tuple(sorted({item.policy for item in scored})),
        bss_vs_climatology=(
            brier_skill_score(score, climatology_brier)
            if climatology_brier not in (None, 0.0)
            else None
        ),
        bss_vs_direct=(
            brier_skill_score(score, direct_brier) if direct_brier not in (None, 0.0) else None
        ),
        climatology_brier=climatology_brier,
        brier_recalibrated=(recalibrated_brier(scored, salt=salt) if salt else None),
    )


def dispersion_finding(
    scored: Sequence[ScoredForecast],
    *,
    sigma_threshold: float,
    seed: int,
    permutations: int = 10_000,
) -> DispersionFinding:
    """§9.2's claim that sigma is informative, tested rather than assumed.

    Two things are reported and they answer different questions. The
    correlation between sigma and absolute error asks whether dispersion
    *ranks* the forecasts by how wrong they are. The Brier of the flagged and
    unflagged subsets asks whether the flag the system actually raises picks
    out the forecasts it should distrust -- which is the operational claim, and
    the one that can be true while the correlation is weak.

    The expected and defensible finding is that flagged scenarios score
    materially worse. If they do not, that is the result, and it is reported as
    measured.
    """
    sigmas = [item.sigma for item in scored]
    errors = [item.abs_error for item in scored]
    flagged = [item for item in scored if item.modality == "multi"]
    unflagged = [item for item in scored if item.modality != "multi"]

    def subset_brier(subset: Sequence[ScoredForecast]) -> float | None:
        if not subset:
            return None
        forecasts, outcomes = _columns(subset)
        return brier(forecasts, outcomes)

    rho = spearman(sigmas, errors)
    r = pearson(sigmas, errors)
    return DispersionFinding(
        n=len(scored),
        pearson_r=r,
        pearson_p=(
            permutation_p(sigmas, errors, seed=seed, permutations=permutations, rank_based=False)
            if r is not None
            else None
        ),
        spearman_rho=rho,
        spearman_p=(
            permutation_p(sigmas, errors, seed=seed, permutations=permutations, rank_based=True)
            if rho is not None
            else None
        ),
        flagged=len(flagged),
        brier_flagged=subset_brier(flagged),
        brier_unflagged=subset_brier(unflagged),
        sigma_threshold=sigma_threshold,
    )


def climatology_reference(scored: Sequence[ScoredForecast], *, base_rate: float) -> float:
    """The sealed climatology forecast, scored on exactly these scenarios.

    Preserves like-for-like skill scores. §10.2's floor is the *sealed* base
    rate issued as a constant forecast -- never a base rate re-estimated from
    whatever is loaded, which would let the floor see the labels it is scored
    on. But a skill score on a partition, or on a 90-scenario capped cell, has
    to be taken against that forecast's Brier **on the same scenarios**; the
    sealed set's own figure is a different population's. At a base rate of
    exactly one half the two coincide on every subset, which is why this never
    showed.
    """
    return brier([base_rate] * len(scored), [item.outcome for item in scored])


def paired_reference(
    scored: Sequence[ScoredForecast], reference: Sequence[ScoredForecast]
) -> float | None:
    """A reference configuration's Brier on the scenarios ``scored`` covers.

    ``None`` when the two share no scenario: not measured, never zero.
    """
    theirs = {item.scenario_id: item for item in reference}
    shared = sorted({item.scenario_id for item in scored} & set(theirs))
    if not shared:
        return None
    return brier([theirs[key].p_hat for key in shared], [theirs[key].outcome for key in shared])


def measure(
    scored: Sequence[ScoredForecast],
    *,
    config_id: str,
    base_rate: float | None,
    direct: Sequence[ScoredForecast] = (),
    salt: str | None = None,
    bins: int = CALIBRATION_BINS,
) -> MetricSet | None:
    """Every metric for one configuration on one already-selected set.

    Preserves one rule: **every reference is taken on the scenarios being
    measured.** Hand it a partition and the climatology floor, the
    single-model reference and the recalibration halves are all drawn from
    inside that partition. ``None`` for an empty set.
    """
    if not scored:
        return None
    return metrics_for(
        scored,
        config_id=config_id,
        climatology_brier=(
            climatology_reference(scored, base_rate=base_rate) if base_rate is not None else None
        ),
        direct_brier=paired_reference(scored, direct) if direct else None,
        salt=salt,
        bins=bins,
    )


def headline_by_partition(
    scored: Sequence[ScoredForecast],
    declaration: SplitDeclaration,
    *,
    config_id: str,
    base_rate: float | None = None,
    direct: Sequence[ScoredForecast] = (),
    salt: str | None = None,
    bins: int = CALIBRATION_BINS,
) -> tuple[tuple[Partition, MetricSet | None], ...]:
    """One configuration measured on ``test``, ``all`` and ``dev``, in that order.

    Preserves the separation the split exists for. Each partition's metrics
    are computed from that partition's forecasts and nothing else -- including
    the isotonic recalibration, whose fit and held-out halves are both drawn
    from inside the partition being measured -- so the ``test`` row is a
    function of test forecasts and test labels alone, and no dev forecast,
    however it was tuned, can move it. ``None`` where a partition holds no
    scored scenario: not measured, never zero.
    """
    partitions: tuple[Partition, ...] = ("test", "all", "dev")
    return tuple(
        (
            partition,
            measure(
                select(scored, declaration, partition),
                config_id=config_id,
                base_rate=base_rate,
                direct=direct,
                salt=salt,
                bins=bins,
            ),
        )
        for partition in partitions
    )


def compare_family(
    specs: Sequence[ComparisonSpec],
    scored_by_config: Mapping[str, Sequence[ScoredForecast]],
    *,
    salt: str,
    b_resamples: int,
    family: ComparisonFamily,
) -> tuple[Comparison, ...]:
    """Paired bootstrap for every comparison in **one** family, Holm-adjusted.

    Preserves two things. Pairing: each comparison is computed on the scenarios
    both sides scored, and that count travels with the result -- the capped
    cells run 90 scenarios against the headline's 180, fewer once restricted to
    a partition, and an unpaired difference would be a difference between two
    populations. And the family boundary: the adjustment sees exactly the
    comparisons passed in, so what is corrected together is decided by the
    caller in the open and not by whatever else happens to be stored.

    A comparison with fewer than two paired scenarios is dropped before the
    adjustment, not after: it cannot be tested, and an untestable hypothesis
    in the family inflates the multiplier on every real one.
    """
    built: list[Comparison] = []
    for spec in specs:
        left = {item.scenario_id: item for item in scored_by_config.get(spec.config_a, ())}
        right = {item.scenario_id: item for item in scored_by_config.get(spec.config_b, ())}
        shared = sorted(set(left) & set(right))
        if len(shared) < 2:
            continue
        pa = [left[key].p_hat for key in shared]
        pb = [right[key].p_hat for key in shared]
        outcomes = [left[key].outcome for key in shared]
        built.append(
            Comparison(
                name=spec.name,
                config_a=spec.config_a,
                config_b=spec.config_b,
                brier_a=brier(pa, outcomes),
                brier_b=brier(pb, outcomes),
                n_paired=len(shared),
                interval=paired_bootstrap(
                    pa,
                    pb,
                    outcomes,
                    seed=bootstrap_seed(salt, spec.config_a, spec.config_b),
                    b_resamples=b_resamples,
                ),
                family=family,
            )
        )
    return adjust_family(built)


def significance_families(
    scored_by_config: Mapping[str, Sequence[ScoredForecast]],
    *,
    headline: str,
    salt: str,
    b_resamples: int,
    eligible: Collection[str],
    exploratory: Sequence[str] = (),
    declaration: SplitDeclaration | None = None,
) -> tuple[Comparison, ...]:
    """Every reported comparison, each adjusted inside its own family.

    Three families, in the order they are printed:

    * ``appendix_c`` -- §10.4's: the named readings plus every *eligible*
      configuration against the headline. ``eligible`` is the declared cells
      and baselines, so nothing else that happens to have forecasts can raise
      the multiplier on the twelve.
    * ``supplementary`` -- comparisons declared in ``eval/supplementary.py``.
    * ``exploratory_dev`` -- tuning variants named by the caller, against the
      headline.

    Preserves the invariant the tests assert with exact equality: **the
    ``appendix_c`` rows are identical whether or not any other family has
    members.** Supplementary ids are removed from the first family even if a
    caller lists them as eligible, so one comparison can never be adjusted
    under two multipliers.

    The exploratory family is refused unless every forecast on both sides of
    it is a dev scenario. It is checked against the data handed in, not
    against a flag saying which partition the caller meant to select: the
    failure this guards against is precisely a caller that forgot to.
    """
    available = sorted(name for name, rows in sorted(scored_by_config.items()) if rows)
    closed = set(eligible) - set(supplementary_ids())
    redundant = sorted(set(exploratory) & (closed | set(supplementary_ids())))
    if redundant:
        raise ValueError(
            f"{redundant} are declared study configurations and are already compared "
            "in their own family; an exploratory comparison is for a tuning variant"
        )
    out = list(
        compare_family(
            comparison_family(available=available, headline=headline, eligible=closed),
            scored_by_config,
            salt=salt,
            b_resamples=b_resamples,
            family="appendix_c",
        )
    )
    out.extend(
        compare_family(
            supplementary_family(available=available, headline=headline),
            scored_by_config,
            salt=salt,
            b_resamples=b_resamples,
            family="supplementary",
        )
    )
    variants = [name for name in sorted(set(exploratory)) if name != headline]
    if variants:
        if declaration is None:
            raise ValueError("exploratory comparisons need the split declaration to be checked")
        for name in [headline, *variants]:
            require_dev_only(
                (item.scenario_id for item in scored_by_config.get(name, ())),
                declaration,
                what=f"exploratory comparison involving {name!r}",
            )
        out.extend(
            compare_family(
                [
                    ComparisonSpec(
                        name=f"{name} vs {headline} (exploratory, dev)",
                        config_a=name,
                        config_b=headline,
                        reading=(
                            f"Brier({name}) - Brier({headline}) on the dev partition. "
                            "A tuning comparison: it may inform a decision and is never "
                            "a result."
                        ),
                    )
                    for name in variants
                    if name in available and headline in available
                ],
                scored_by_config,
                salt=salt,
                b_resamples=b_resamples,
                family="exploratory_dev",
            )
        )
    return tuple(out)

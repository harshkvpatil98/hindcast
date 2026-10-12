"""Blending the simulation's forecast with a reference forecast (M14).

Two forecasters that err differently can be averaged into one that errs less.
The simulation reasons from structure -- who can move what, and who wants to --
and a single model asked directly reasons from the text. Where they disagree,
neither is reliably the better one, which is the condition under which a
weighted average helps.

The weight is a fitted parameter, and a parameter fitted on the scenarios it is
then scored on makes the score a description of the fit. So the weight is
fitted on the **development partition** and applied, unchanged, to the test
partition: :func:`fit_weight` and :func:`apply_weight` are separate functions
with separate inputs so that the one cannot see the other's data.

The pool is linear in probability, for which the Brier-optimal weight has a
closed form. No optimiser, no dependency, no tolerance, and the fit is exactly
reproducible -- sums are exactly rounded, so not even the order the scenarios
are keyed in can move the weight.

Pure: no I/O, no clock, and no RNG but the seeded bootstrap the caller keys.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from cascade.eval.schema import BootstrapInterval
from cascade.eval.stats import bootstrap_differences

__all__ = ["BlendEvaluation", "BlendFit", "Paired", "apply_weight", "evaluate_blend", "fit_weight"]


class Paired(BaseModel):
    """One scenario seen by both forecasters, with its outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system: float = Field(ge=0.0, le=1.0)
    reference: float = Field(ge=0.0, le=1.0)
    outcome: int = Field(ge=0, le=1)


class BlendFit(BaseModel):
    """A fitted weight and the evidence it was fitted on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    weight: float = Field(ge=0.0, le=1.0)
    """Share given to the system's forecast; the reference gets the rest."""
    unclipped: float | None
    """The unconstrained optimum, or None when the two forecasters never
    differed. Outside [0, 1] it says one forecaster is best *subtracted* from
    the other -- reported, and never applied, because a negative weight can
    leave the unit interval."""
    n: int = Field(ge=0)
    brier_system: float | None
    brier_reference: float | None
    brier_blend: float | None
    """All three on the fitting data. The blend's is optimistic by
    construction; the honest figure is the one computed on other scenarios."""


def _exact_sum(values: Sequence[float]) -> float:
    """An exactly rounded sum, so the fit is a function of the *set* of
    scenarios: relabelling them, which reorders the terms, cannot move the
    weight by a unit in the last place."""
    return math.fsum(values)


def _mean(values: Sequence[float]) -> float | None:
    return _exact_sum(values) / len(values) if values else None


def fit_weight(pairs: Mapping[str, Paired]) -> BlendFit:
    """The weight on the system's forecast that minimises Brier over ``pairs``.

    With ``b = w*s + (1-w)*r`` the Brier is quadratic in ``w`` and its minimum
    is ``sum((s-r)*(y-r)) / sum((s-r)^2)``, clipped to [0, 1]. When the two
    forecasters never differ the weight is unidentified; it is returned as 1.0
    -- the system alone -- so an uninformative fit cannot quietly introduce a
    second forecaster.

    Preserves the separation the blend is only honest under: this function
    sees the pairs it is given and nothing else. The caller passes the
    development partition; nothing here can reach the test partition.
    """
    keys = sorted(pairs)
    system = [pairs[key].system for key in keys]
    reference = [pairs[key].reference for key in keys]
    outcome = [float(pairs[key].outcome) for key in keys]

    denominator = _exact_sum([(s - r) ** 2 for s, r in zip(system, reference, strict=True)])
    if denominator > 0.0:
        unclipped: float | None = (
            _exact_sum(
                [(s - r) * (y - r) for s, r, y in zip(system, reference, outcome, strict=True)]
            )
            / denominator
        )
        weight = min(1.0, max(0.0, unclipped if unclipped is not None else 1.0))
    else:
        unclipped = None
        weight = 1.0

    blended = [weight * s + (1.0 - weight) * r for s, r in zip(system, reference, strict=True)]
    return BlendFit(
        weight=weight,
        unclipped=unclipped,
        n=len(keys),
        brier_system=_mean([(s - y) ** 2 for s, y in zip(system, outcome, strict=True)]),
        brier_reference=_mean([(r - y) ** 2 for r, y in zip(reference, outcome, strict=True)]),
        brier_blend=_mean([(b - y) ** 2 for b, y in zip(blended, outcome, strict=True)]),
    )


def apply_weight(weight: float, forecasts: Mapping[str, tuple[float, float]]) -> dict[str, float]:
    """Blend ``(system, reference)`` forecasts under an already-fitted weight.

    Takes no outcomes, by signature: the scenarios a blend is scored on must
    have had no say in its weight, and a function that cannot receive their
    labels cannot use them.
    """
    if not 0.0 <= weight <= 1.0:
        raise ValueError(f"weight must lie in [0, 1], got {weight}")
    return {
        key: weight * forecasts[key][0] + (1.0 - weight) * forecasts[key][1]
        for key in sorted(forecasts)
    }


class BlendEvaluation(BaseModel):
    """A weight fitted on one partition and scored, unchanged, on another."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fit: BlendFit
    n_scored: int = Field(ge=0)
    brier_system: float | None
    brier_reference: float | None
    brier_blend: float | None
    blend_minus_system: BootstrapInterval | None
    """Paired over the scored scenarios. Negative means the blend is better."""


def evaluate_blend(
    fitting: Mapping[str, Paired],
    scored: Mapping[str, Paired],
    *,
    seed: int,
    b_resamples: int,
) -> BlendEvaluation:
    """Fit on ``fitting``, score on ``scored``; the two must not overlap.

    Preserves the separation the blend's reported gain depends on: a scenario
    in both sets would let the weight be tuned on a scenario it is then scored
    on, so an overlap raises rather than quietly counting twice. The scored
    side reaches :func:`apply_weight` without its outcomes; they are used only
    afterwards, to score.
    """
    overlap = sorted(set(fitting) & set(scored))
    if overlap:
        raise ValueError(
            f"{len(overlap)} scenario(s) are in both the fitting and the scored set "
            f"(first: {overlap[0]}); a blend weight must not be scored where it was fitted"
        )
    fit = fit_weight(fitting)
    blended = apply_weight(
        fit.weight, {key: (scored[key].system, scored[key].reference) for key in sorted(scored)}
    )
    keys = sorted(scored)
    outcome = {key: float(scored[key].outcome) for key in keys}
    system_errors = [(scored[key].system - outcome[key]) ** 2 for key in keys]
    blend_errors = [(blended[key] - outcome[key]) ** 2 for key in keys]
    differences = [b - s for b, s in zip(blend_errors, system_errors, strict=True)]
    interval = (
        bootstrap_differences(
            differences,
            point=_exact_sum(differences) / len(differences),
            seed=seed,
            b_resamples=b_resamples,
        )
        if differences
        else None
    )
    return BlendEvaluation(
        fit=fit,
        n_scored=len(keys),
        brier_system=_mean(system_errors),
        brier_reference=_mean([(scored[key].reference - outcome[key]) ** 2 for key in keys]),
        brier_blend=_mean(blend_errors),
        blend_minus_system=interval,
    )

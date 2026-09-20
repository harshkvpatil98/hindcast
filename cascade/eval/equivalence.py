"""Do two providers serve the same model? ADR-0029's condition, measured.

The three API providers share one cache namespace: a recording made through
Anthropic replays through Bedrock. That is only sound if both serve the same
model, and "same model" cannot be checked by comparing single answers --
temperature makes even one provider disagree with itself. So the probe carries
its own control. For each question the reference provider answers twice and
the candidate once:

* ``within = |a1 - a2|`` -- how much the reference disagrees with itself;
* ``cross  = |a1 - b|``  -- how much the candidate disagrees with it.

If both serve one model, ``a2`` and ``b`` are exchangeable given ``a1`` and
the mean of ``cross - within`` is zero. The paired bootstrap from
:mod:`cascade.eval.stats` puts an interval on it; an interval wholly above zero
is divergence, and ADR-0029 then requires the provider to join the cache key.

An interval that includes zero is reported as *no divergence detected*, not as
equivalence: at small n it may only mean the probe was underpowered, and the
report carries n and the interval so a reader can tell which.

Questions come from the memorization probe (:func:`probe_request`), loaded as
``cascade_sim``. The probe compares providers with each other and never with
the outcome, so it has no reason to hold a label and is built so it cannot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from cascade.config import LLMProvider, Settings
from cascade.eval.schema import BootstrapInterval
from cascade.eval.stats import bootstrap_differences, bootstrap_seed
from cascade.ledger.schema import Scenario
from cascade.retrieval.memorization import parse_probability, probe_request

__all__ = ["EquivalenceReport", "assess", "run_equivalence"]

Triple = tuple[float | None, float | None, float | None]


@dataclass(frozen=True)
class EquivalenceReport:
    """The measured answer to "is this the same model?" for two providers."""

    reference: str
    candidate: str
    asked: int
    scored: int
    within_mean: float
    cross_mean: float
    interval: BootstrapInterval

    @property
    def dropped(self) -> int:
        """Questions with an unparseable answer from either side, never scored."""
        return self.asked - self.scored

    @property
    def divergent(self) -> bool:
        """The candidate disagrees with the reference more than it does with itself."""
        return self.interval.lo > 0.0


def assess(
    answers: Sequence[Triple],
    *,
    reference: str,
    candidate: str,
    seed: int,
    b_resamples: int = 10_000,
) -> EquivalenceReport:
    """Score ``(a1, a2, b)`` answer triples. Pure.

    Preserves the invariant that an unparseable answer is dropped and counted,
    never imputed: scoring it as 0.5 would make two providers that both fail
    to answer look like perfect agreement.
    """
    kept = [
        (float(a1), float(a2), float(b))
        for a1, a2, b in answers
        if a1 is not None and a2 is not None and b is not None
    ]
    if not kept:
        raise ValueError(
            f"no scoreable answers from {len(answers)} question(s): every triple had an "
            "unparseable answer, so the providers cannot be compared"
        )
    within = np.array([abs(a1 - a2) for a1, a2, _ in kept])
    cross = np.array([abs(a1 - b) for a1, _, b in kept])
    per_question = cross - within
    interval = bootstrap_differences(
        per_question,
        point=float(np.mean(per_question)),
        seed=seed,
        b_resamples=b_resamples,
    )
    return EquivalenceReport(
        reference=reference,
        candidate=candidate,
        asked=len(answers),
        scored=len(kept),
        within_mean=float(np.mean(within)),
        cross_mean=float(np.mean(cross)),
        interval=interval,
    )


def _live(settings: Settings, provider: LLMProvider) -> Settings:
    """``settings`` sending fresh calls to ``provider``.

    Live, never record or replay: the API providers share a cache namespace,
    which is the very thing under test, so a cached run would answer the
    candidate's question with the reference's recording and report perfect
    agreement by construction.
    """
    return settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"provider": provider, "mode": "live"})}
    )


def run_equivalence(
    settings: Settings,
    scenarios: Sequence[Scenario],
    *,
    reference: LLMProvider,
    candidate: LLMProvider,
    b_resamples: int = 10_000,
) -> EquivalenceReport:
    """Ask every question three times across two providers and assess the answers."""
    from cascade.llm.client import LLMClient

    ref = LLMClient(_live(settings, reference), phase="bench")
    cand = LLMClient(_live(settings, candidate), phase="bench")
    triples: list[Triple] = []
    for scenario in sorted(scenarios, key=lambda item: item.scenario_id):
        request = probe_request(settings, scenario)
        a1 = parse_probability(ref.complete(request, trace_name="equivalence.reference").text)
        a2 = parse_probability(ref.complete(request, trace_name="equivalence.reference").text)
        b = parse_probability(cand.complete(request, trace_name="equivalence.candidate").text)
        triples.append((a1, a2, b))
    return assess(
        triples,
        reference=reference,
        candidate=candidate,
        seed=bootstrap_seed(settings.study.salt, "provider-equivalence", reference, candidate),
        b_resamples=b_resamples,
    )

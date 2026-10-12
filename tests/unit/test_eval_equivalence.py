"""The provider-equivalence probe (ADR-0029).

The probe is only worth running if it can tell the two outcomes apart, so both
are constructed here: a candidate drawn from the reference's own distribution,
which must *not* be called divergent, and one offset from it, which must be.
"""

from __future__ import annotations

import numpy as np
import pytest

from cascade.config import Settings
from cascade.eval.equivalence import _live, assess


def draws(seed: int, n: int, *, offset: float = 0.0) -> list[tuple[float, float, float]]:
    """(a1, a2, b): two reference answers and one candidate answer per question."""
    rng = np.random.default_rng(seed)
    truth = rng.uniform(0.2, 0.8, n)
    noise = lambda: rng.normal(0.0, 0.05, n)  # noqa: E731 -- local, three uses
    a1, a2 = truth + noise(), truth + noise()
    b = truth + offset + noise()
    return [(float(x), float(y), float(z)) for x, y, z in zip(a1, a2, b, strict=True)]


def test_a_candidate_serving_the_same_model_is_not_called_divergent() -> None:
    report = assess(draws(1, 200), reference="anthropic", candidate="bedrock", seed=11)
    assert not report.divergent
    assert report.interval.lo <= 0.0 <= report.interval.hi


def test_a_candidate_serving_a_different_model_is_called_divergent() -> None:
    report = assess(draws(2, 200, offset=0.15), reference="anthropic", candidate="bedrock", seed=11)
    assert report.divergent
    assert report.cross_mean > report.within_mean


def test_an_unparseable_answer_is_dropped_and_counted_never_imputed() -> None:
    triples = [*draws(3, 40), (0.5, None, 0.5), (None, 0.4, 0.4)]
    report = assess(triples, reference="anthropic", candidate="aws", seed=11)
    assert report.asked == 42
    assert report.scored == 40
    assert report.dropped == 2


def test_nothing_scoreable_is_an_error_not_agreement() -> None:
    with pytest.raises(ValueError, match="no scoreable answers"):
        assess([(None, None, None)], reference="anthropic", candidate="aws", seed=11)


def test_the_interval_is_reproducible_from_the_seed() -> None:
    first = assess(draws(4, 60), reference="anthropic", candidate="aws", seed=99)
    second = assess(draws(4, 60), reference="anthropic", candidate="aws", seed=99)
    assert first.interval == second.interval


def test_the_probe_runs_live_so_the_shared_cache_cannot_answer_for_the_candidate(
    settings: Settings,
) -> None:
    """Record or replay would serve the candidate the reference's recording."""
    live = _live(settings, "bedrock")
    assert live.llm.mode == "live"
    assert live.llm.provider == "bedrock"

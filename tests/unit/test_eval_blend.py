"""The forecast blend (M14): fitted on one set of scenarios, applied to another."""

from __future__ import annotations

import inspect

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cascade.eval.blend import Paired, apply_weight, fit_weight


def pairs(rows: list[tuple[float, float, int]]) -> dict[str, Paired]:
    return {
        f"s{index:03d}": Paired(system=s, reference=r, outcome=y)
        for index, (s, r, y) in enumerate(rows)
    }


def brier(weight: float, rows: list[tuple[float, float, int]]) -> float:
    return sum((weight * s + (1 - weight) * r - y) ** 2 for s, r, y in rows) / len(rows)


def test_a_perfect_system_gets_the_whole_weight() -> None:
    fit = fit_weight(pairs([(1.0, 0.5, 1), (0.0, 0.5, 0), (1.0, 0.4, 1)]))
    assert fit.weight == 1.0
    assert fit.brier_blend == 0.0


def test_a_perfect_reference_gets_the_whole_weight() -> None:
    fit = fit_weight(pairs([(0.5, 1.0, 1), (0.5, 0.0, 0)]))
    assert fit.weight == 0.0


def test_complementary_errors_are_averaged_and_beat_both() -> None:
    rows = [(0.9, 0.5, 1), (0.5, 0.1, 0), (0.5, 0.9, 1), (0.1, 0.5, 0)]
    fit = fit_weight(pairs(rows))
    assert fit.weight == pytest.approx(0.5)
    assert fit.brier_blend is not None and fit.brier_system is not None
    assert fit.brier_blend < min(fit.brier_system, fit.brier_reference or 1.0)


def test_an_optimum_outside_the_unit_interval_is_reported_and_clipped() -> None:
    """The reference is worse than useless here; the best linear use of it is
    to subtract it, which could leave [0, 1]. Reported, never applied."""
    fit = fit_weight(pairs([(0.8, 0.2, 1), (0.2, 0.8, 0), (0.9, 0.3, 1)]))
    assert fit.unclipped is not None and fit.unclipped > 1.0
    assert fit.weight == 1.0


def test_identical_forecasters_leave_the_system_alone() -> None:
    fit = fit_weight(pairs([(0.6, 0.6, 1), (0.3, 0.3, 0)]))
    assert fit.unclipped is None
    assert fit.weight == 1.0


def test_no_pairs_is_not_an_error_and_not_a_fit() -> None:
    fit = fit_weight({})
    assert (fit.n, fit.weight, fit.brier_blend) == (0, 1.0, None)


probability = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
row = st.tuples(probability, probability, st.integers(min_value=0, max_value=1))


@given(st.lists(row, min_size=1, max_size=30), probability)
def test_no_other_weight_in_the_interval_does_better(
    rows: list[tuple[float, float, int]], other: float
) -> None:
    fit = fit_weight(pairs(rows))
    assert brier(fit.weight, rows) <= brier(other, rows) + 1e-12


@given(st.lists(row, min_size=1, max_size=30))
def test_the_fit_does_not_depend_on_the_order_scenarios_arrive_in(
    rows: list[tuple[float, float, int]],
) -> None:
    forward = pairs(rows)
    backward = dict(reversed(list(forward.items())))
    assert fit_weight(forward) == fit_weight(backward)


def test_relabelling_the_scenarios_cannot_move_the_weight() -> None:
    """One large term and ten tiny ones: a running float sum keeps the tiny
    ones only if they come first. The fit must not care."""
    big, tiny = (1.0, 0.0, 1), (1e-8, 0.0, 1)
    assert fit_weight(pairs([big] + [tiny] * 10)) == fit_weight(pairs([tiny] * 10 + [big]))


def test_applying_a_weight_cannot_see_an_outcome() -> None:
    """The separation is structural: the function that blends the scored
    scenarios has no parameter an outcome could arrive through."""
    parameters = inspect.signature(apply_weight).parameters
    assert list(parameters) == ["weight", "forecasts"]
    assert apply_weight(0.25, {"b": (0.8, 0.4), "a": (0.0, 1.0)}) == {"a": 0.75, "b": 0.5}


def test_a_weight_outside_the_interval_is_refused() -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        apply_weight(1.2, {"a": (0.5, 0.5)})

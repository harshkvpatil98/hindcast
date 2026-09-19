"""Accuracy by evidence quality (`cascade/eval/evidence.py`).

Declared before any forecast existed. What is tested is what keeps it that
way: the tier boundaries are fixed chunk counts and not functions of the data,
every scenario lands in exactly one tier, an unmeasured scenario is never
filed as "no evidence", and the one test of the claim ignores the tiers.
"""

from __future__ import annotations

import pytest

from cascade.eval.evidence import (
    EVIDENCE_WINDOW_DAYS,
    TIERS,
    evidence_finding,
    per_tier,
    tier_of,
)
from cascade.eval.metrics import brier
from cascade.eval.schema import ScoredForecast


def _scored(rows: list[tuple[str, float, int]]) -> tuple[ScoredForecast, ...]:
    return tuple(
        ScoredForecast(scenario_id=sid, config_id="C01", p_hat=p, outcome=y, domain="elections")  # type: ignore[arg-type]
        for sid, p, y in rows
    )


class TestTheTiersAreDeclaredConstants:
    def test_the_thresholds(self) -> None:
        """Read off the distribution measured on 2026-09-19 -- 7 scenarios with
        none, 75 with 1-999, 15 with 1,000-9,999, 83 with 10,000+ -- and fixed.
        Changing one is a commit, and this is the line it has to change."""
        assert [(tier.name, tier.lo, tier.hi) for tier in TIERS] == [
            ("none", 0, 0),
            ("thin", 1, 999),
            ("moderate", 1_000, 9_999),
            ("rich", 10_000, None),
        ]
        assert EVIDENCE_WINDOW_DAYS == 30

    @pytest.mark.parametrize(
        ("count", "tier"),
        [
            (0, "none"),
            (1, "thin"),
            (999, "thin"),
            (1_000, "moderate"),
            (9_999, "moderate"),
            (10_000, "rich"),
            (2_500_000, "rich"),
        ],
    )
    def test_each_boundary_falls_where_it_was_declared(self, count: int, tier: str) -> None:
        assert tier_of(count).name == tier

    def test_the_tiers_partition_the_counts(self) -> None:
        for count in [*range(0, 1_200), *range(9_900, 10_100)]:
            assert sum(tier.holds(count) for tier in TIERS) == 1

    def test_a_negative_count_is_a_broken_query_not_thin_evidence(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            tier_of(-1)

    def test_they_are_not_configuration(self) -> None:
        """A threshold an environment variable can move is a threshold that can
        be moved after looking, without a trace in the repository."""
        from cascade.config import load_settings

        assert "tier" not in " ".join(type(load_settings().eval).model_fields)


class TestTiersDoNotDependOnTheData:
    """The mutation this guards against is tiering by quantile."""

    def test_a_scenario_s_tier_is_a_function_of_its_own_count(self) -> None:
        one = per_tier(_scored([("a", 0.2, 0)]), {"a": 500})
        crowd = per_tier(
            _scored([("a", 0.2, 0), ("b", 0.3, 0), ("c", 0.4, 1), ("d", 0.6, 1)]),
            {"a": 500, "b": 5, "c": 7, "d": 9},
        )
        assert {row.tier: row.n for row in one}["thin"] == 1
        assert {row.tier: row.n for row in crowd}["thin"] == 4

    def test_a_corpus_where_everything_is_rich_has_three_empty_tiers(self) -> None:
        """Quantile tiers would split these evenly and call a quarter of them
        evidence-poor. They are not."""
        rows = per_tier(
            _scored([(f"s{i}", 0.5, i % 2) for i in range(8)]),
            {f"s{i}": 20_000 + 1_000 * i for i in range(8)},
        )
        assert [(row.tier, row.n) for row in rows] == [
            ("none", 0),
            ("thin", 0),
            ("moderate", 0),
            ("rich", 8),
        ]

    def test_scaling_every_count_moves_scenarios_across_fixed_lines(self) -> None:
        scored = _scored([(f"s{i}", 0.5, i % 2) for i in range(6)])
        counts = {f"s{i}": 10**i for i in range(6)}  # 1 .. 100,000
        assert [row.n for row in per_tier(scored, counts)] == [0, 3, 1, 2]
        grown = {key: value * 100 for key, value in sorted(counts.items())}
        assert [row.n for row in per_tier(scored, grown)] == [0, 1, 1, 4]


class TestTheTable:
    def test_every_declared_tier_is_a_row_even_when_empty(self) -> None:
        rows = per_tier(_scored([("a", 0.9, 1)]), {"a": 0})
        assert [row.tier for row in rows] == ["none", "thin", "moderate", "rich"]
        empty = rows[1]
        assert (empty.n, empty.base_rate, empty.brier) == (0, None, None)

    def test_a_tier_s_brier_is_the_brier_of_its_scenarios(self) -> None:
        scored = _scored(
            [("a", 0.9, 1), ("b", 0.2, 1), ("c", 0.4, 0), ("d", 0.7, 0), ("e", 0.1, 0)]
        )
        counts = {"a": 0, "b": 50, "c": 900, "d": 4_000, "e": 50_000}
        rows = {row.tier: row for row in per_tier(scored, counts)}
        assert rows["thin"].n == 2
        assert rows["thin"].brier == brier([0.2, 0.4], [1, 0])
        assert rows["thin"].base_rate == 0.5
        assert rows["none"].brier == brier([0.9], [1])
        assert rows["rich"].brier == brier([0.1], [0])
        assert sum(row.n for row in rows.values()) == len(scored)

    def test_the_rows_carry_their_bounds(self) -> None:
        rows = per_tier(_scored([("a", 0.9, 1)]), {"a": 3})
        assert [(row.lo, row.hi) for row in rows] == [(0, 0), (1, 999), (1000, 9999), (10000, None)]

    def test_an_unmeasured_scenario_is_refused_not_filed_under_none(self) -> None:
        with pytest.raises(ValueError, match="no measured evidence count"):
            per_tier(_scored([("a", 0.9, 1), ("b", 0.1, 0)]), {"a": 10})

    def test_input_order_does_not_matter(self) -> None:
        scored = _scored([("a", 0.9, 1), ("b", 0.2, 1), ("c", 0.4, 0)])
        counts = {"a": 1, "b": 2, "c": 3}
        assert per_tier(scored, counts) == per_tier(tuple(reversed(scored)), counts)


class TestTheOneDeclaredTest:
    def test_more_evidence_with_smaller_error_is_a_negative_rho(self) -> None:
        scored = _scored([(f"s{i}", 0.5 + 0.05 * i, 1) for i in range(9)])
        counts = {f"s{i}": 10 * (i + 1) for i in range(9)}
        finding = evidence_finding(scored, counts, seed=7, permutations=500)
        assert finding.spearman_rho == pytest.approx(-1.0)
        assert finding.spearman_p is not None and finding.spearman_p < 0.05
        assert finding.window_days == 30 and finding.n == 9

    def test_it_is_computed_on_counts_so_the_tiers_cannot_influence_it(self) -> None:
        """Every scenario here is in one tier; the correlation is still defined."""
        scored = _scored([(f"s{i}", 0.5 + 0.05 * i, 1) for i in range(9)])
        counts = {f"s{i}": 20_000 + i for i in range(9)}
        finding = evidence_finding(scored, counts, seed=7, permutations=200)
        assert [row.n for row in finding.tiers] == [0, 0, 0, 9]
        assert finding.spearman_rho == pytest.approx(-1.0)

    def test_constant_counts_are_undefined_not_zero(self) -> None:
        scored = _scored([("a", 0.9, 1), ("b", 0.2, 1), ("c", 0.4, 0)])
        finding = evidence_finding(scored, {"a": 5, "b": 5, "c": 5}, seed=1, permutations=50)
        assert finding.spearman_rho is None and finding.spearman_p is None

    def test_it_is_reproducible_from_its_seed(self) -> None:
        scored = _scored([(f"s{i}", (i * 37 % 100) / 100, i % 2) for i in range(20)])
        counts = {f"s{i}": (i * 7919) % 30_000 for i in range(20)}
        assert evidence_finding(scored, counts, seed=3, permutations=300) == evidence_finding(
            scored, counts, seed=3, permutations=300
        )

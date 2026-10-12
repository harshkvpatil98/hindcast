"""Supplementary comparisons (`cascade/eval/supplementary.py`).

S01 is the headline configuration with 12 evidence chunks per agent instead of
6, declared before any forecast existed. Three failure modes are tested for:
the overlay quietly changing more than it says, the cell executing identically
to the headline (ADR-0025's inert switch), and -- the one that would damage
results that have nothing to do with it -- S01 entering Appendix C's Holm
family and weakening all twelve ablation comparisons.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from cascade.config import load_settings, overlay_kind, repo_root
from cascade.eval.ablation import CELLS, comparison_family
from cascade.eval.schema import ScoredForecast
from cascade.eval.score import significance_families
from cascade.eval.split import HeldOutViolation, declare_split
from cascade.eval.supplementary import (
    OVERRIDES,
    SUPPLEMENTARY_CELLS,
    supplementary_by_id,
    supplementary_family,
    supplementary_ids,
)

SUPPLEMENTARY_DIR = repo_root() / "configs" / "supplementary"
APPENDIX_C = frozenset(
    {cell.cell_id for cell in CELLS} | {"B1_climatology", "B2_single_direct", "B3_self_consistency"}
)


def _flatten(prefix: str, value: object) -> dict[str, object]:
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, inner in sorted(value.items()):
            out.update(_flatten(f"{prefix}.{key}" if prefix else str(key), inner))
        return out
    return {prefix: value}


class TestTheDeclaration:
    def test_s01_is_declared(self) -> None:
        assert supplementary_ids() == ("S01",)
        cell = supplementary_by_id("S01")
        assert (cell.decomposition, cell.information_asymmetry, cell.grounding) == (
            True,
            True,
            "chronofence",
        )
        assert not cell.is_headline

    def test_supplementary_ids_never_collide_with_the_twelve(self) -> None:
        assert set(supplementary_ids()).isdisjoint(cell.cell_id for cell in CELLS)

    def test_an_unknown_cell_names_the_declared_ones(self) -> None:
        with pytest.raises(KeyError, match="S01"):
            supplementary_by_id("S99")

    def test_the_overlays_live_beside_the_twelve_not_among_them(self) -> None:
        """`configs/ablations/` holds exactly Appendix C's twelve, and
        `test_ablation_cells.py` asserts it."""
        assert sorted(path.stem for path in SUPPLEMENTARY_DIR.glob("*.yaml")) == list(
            supplementary_ids()
        )
        assert overlay_kind("S01") == "supplementary"
        assert overlay_kind("C01") == "ablations"
        assert overlay_kind("nothing-by-this-name") is None


class TestTheOverlayChangesOnlyWhatItDeclares:
    @pytest.mark.parametrize("cell", SUPPLEMENTARY_CELLS, ids=lambda cell: cell.cell_id)
    def test_it_differs_from_the_headline_in_exactly_the_declared_settings(self, cell) -> None:
        """Two statements of the design that must agree. An S01 that also
        nudged a kernel constant would not be a comparison of k."""
        headline = _flatten("", json.loads(load_settings("C01").model_dump_json()))
        variant = _flatten("", json.loads(load_settings(cell.cell_id).model_dump_json()))
        differing = {
            key: variant[key] for key in sorted(variant) if variant[key] != headline.get(key)
        }
        assert differing == dict(OVERRIDES[cell.cell_id])

    def test_s01_asks_for_twelve_chunks_against_the_headline_s_six(self) -> None:
        assert load_settings("S01").retrieval.k_agent == 12
        assert load_settings("C01").retrieval.k_agent == 6
        assert load_settings("S01").retrieval.k_agent <= load_settings("S01").retrieval.max_k

    def test_the_setting_is_read_by_the_code_that_retrieves(self) -> None:
        """ADR-0025's check, applied here: a field read only by the module that
        defines it is an inert switch, and S01 would execute as C01."""
        readers = [
            path
            for path in sorted((repo_root() / "cascade").rglob("*.py"))
            if path.name != "config.py" and "retrieval.k_agent" in path.read_text(encoding="utf-8")
        ]
        assert readers, "retrieval.k_agent is read nowhere outside config.py"

    def test_more_chunks_means_a_different_prompt(self) -> None:
        """Behavioural: nothing between retrieval and the prompt truncates the
        evidence back to six."""
        from cascade.sim.prompts import brief_from, persona_block

        class _Actor:
            id = "a1"
            name = "Actor"
            objective = "do something"
            risk_posture = "neutral"
            constraints = ()
            utility_terms = ()

        def block(k: int) -> str:
            evidence = tuple(
                (f"2025-01-{i + 1:02d}", "src", f"excerpt number {i}") for i in range(k)
            )
            return persona_block(
                brief_from(
                    _Actor(),
                    levers={},
                    counterparties=(),
                    horizon=24,
                    question_context="q",
                    evidence=evidence,
                    grounded=True,
                ),
                evidence_chars=900,
            )

        six, twelve = block(6), block(12)
        assert "excerpt number 11" in twelve and "excerpt number 11" not in six
        assert "excerpt number 5" in six


class TestItsOwnHolmFamily:
    def test_the_family_is_s01_against_the_headline(self) -> None:
        family = supplementary_family(available=["C01", "C05", "S01"], headline="C01")
        assert [(spec.config_a, spec.config_b) for spec in family] == [("S01", "C01")]
        assert "own Holm family" in family[0].reading

    def test_it_is_empty_without_both_sides(self) -> None:
        assert supplementary_family(available=["C01", "C05"], headline="C01") == ()
        assert supplementary_family(available=["S01", "C05"], headline="C01") == ()

    def test_a_closed_appendix_c_family_does_not_admit_s01(self) -> None:
        family = comparison_family(
            available=["C01", "C05", "S01", "k9-try3"], headline="C01", eligible=APPENDIX_C
        )
        assert [(spec.config_a, spec.config_b) for spec in family] == [("C05", "C01")]

    def test_the_open_reading_would_have_admitted_it(self) -> None:
        """Why `eligible` exists: left open, anything with forecasts joins."""
        family = comparison_family(available=["C01", "C05", "S01"], headline="C01")
        assert ("S01", "C01") in {(spec.config_a, spec.config_b) for spec in family}


def _forecasts(config_id: str, ids: list[str], shift: float) -> tuple[ScoredForecast, ...]:
    return tuple(
        ScoredForecast(
            scenario_id=scenario_id,
            config_id=config_id,
            p_hat=min(
                1.0, max(0.0, ((index * 37 + len(config_id)) % 100) / 100 * (1 - shift) + shift / 2)
            ),
            outcome=index % 2,  # type: ignore[arg-type]
            domain="elections",
        )
        for index, scenario_id in enumerate(ids)
    )


class TestTheTwelveAreUntouchedByTheSupplementaryFamily:
    IDS: ClassVar[list[str]] = [f"scenario-{index:03d}" for index in range(60)]

    def _grid(self) -> dict[str, tuple[ScoredForecast, ...]]:
        return {
            "C01": _forecasts("C01", self.IDS, 0.0),
            "C05": _forecasts("C05", self.IDS, 0.3),
            "C09": _forecasts("C09", self.IDS, 0.6),
            "C03": _forecasts("C03", self.IDS[:40], 0.2),
            "B2_single_direct": _forecasts("B2_single_direct", self.IDS, 0.5),
        }

    def _run(self, scored: dict[str, tuple[ScoredForecast, ...]]):
        return significance_families(
            scored, headline="C01", salt="salt", b_resamples=400, eligible=APPENDIX_C
        )

    def test_appendix_c_rows_are_exactly_equal_with_and_without_s01(self) -> None:
        """Exact equality, not closeness: same points, same intervals, same raw
        and **adjusted** p-values. Adding a supplementary comparison must not
        move a single digit of an ablation result."""
        without = self._run(self._grid())
        with_s01 = self._run({**self._grid(), "S01": _forecasts("S01", self.IDS, 0.1)})

        main_without = [item for item in without if item.family == "appendix_c"]
        main_with = [item for item in with_s01 if item.family == "appendix_c"]
        assert main_without == main_with
        assert len(main_with) >= 4
        assert [item.p_adjusted for item in main_with] == [item.p_adjusted for item in main_without]

    def test_s01_is_reported_in_a_family_of_its_own(self) -> None:
        results = self._run({**self._grid(), "S01": _forecasts("S01", self.IDS, 0.1)})
        extra = [item for item in results if item.family == "supplementary"]
        assert [(item.config_a, item.config_b) for item in extra] == [("S01", "C01")]
        # A family of one is adjusted by one: Holm leaves its p-value alone.
        assert extra[0].p_adjusted == extra[0].interval.p_value
        assert all(item.config_a != "S01" for item in results if item.family == "appendix_c")

    def test_folding_s01_into_the_twelve_would_have_changed_them(self) -> None:
        """The counterfactual, so the equality above is known to be able to
        fail: one more hypothesis raises the Holm multiplier."""
        from cascade.eval.stats import holm_bonferroni

        results = self._run({**self._grid(), "S01": _forecasts("S01", self.IDS, 0.1)})
        main = [item for item in results if item.family == "appendix_c"]
        extra = [item for item in results if item.family == "supplementary"]
        folded = holm_bonferroni([item.interval.p_value for item in [*main, *extra]])
        assert list(folded[: len(main)]) != [item.p_adjusted for item in main]

    def test_a_stored_tuning_variant_cannot_join_either_family(self) -> None:
        results = self._run({**self._grid(), "k9-try3": _forecasts("k9-try3", self.IDS, 0.1)})
        assert results == self._run(self._grid())

    def test_listing_s01_as_eligible_still_keeps_it_out_of_the_twelve(self) -> None:
        results = significance_families(
            {**self._grid(), "S01": _forecasts("S01", self.IDS, 0.1)},
            headline="C01",
            salt="salt",
            b_resamples=400,
            eligible=APPENDIX_C | {"S01"},
        )
        assert [item for item in results if item.family == "appendix_c"] == [
            item for item in self._run(self._grid()) if item.family == "appendix_c"
        ]


class TestExploratoryComparisonsAreDevOnly:
    PAIRS: ClassVar[list[tuple[str, str]]] = [
        (f"scenario-{index:03d}", "elections" if index % 2 else "sports") for index in range(60)
    ]

    def _declared(self):
        return declare_split(self.PAIRS, salt="salt", dev_size=20)

    def test_a_variant_compared_on_dev_forecasts_is_allowed_and_labelled(self) -> None:
        declared = self._declared()
        dev = list(declared.dev)
        results = significance_families(
            {"C01": _forecasts("C01", dev, 0.0), "k9": _forecasts("k9", dev, 0.2)},
            headline="C01",
            salt="salt",
            b_resamples=200,
            eligible=APPENDIX_C,
            exploratory=("k9",),
            declaration=declared,
        )
        assert [(item.family, item.config_a) for item in results] == [("exploratory_dev", "k9")]
        assert results[0].n_paired == 20

    def test_one_held_out_forecast_on_either_side_refuses_the_lot(self) -> None:
        """Checked against the rows handed in, not against a flag saying which
        partition the caller meant to select."""
        declared = self._declared()
        dev, leaked = list(declared.dev), [*declared.dev, declared.test[0]]
        for scored in (
            {"C01": _forecasts("C01", dev, 0.0), "k9": _forecasts("k9", leaked, 0.2)},
            {"C01": _forecasts("C01", leaked, 0.0), "k9": _forecasts("k9", dev, 0.2)},
        ):
            with pytest.raises(HeldOutViolation):
                significance_families(
                    scored,
                    headline="C01",
                    salt="salt",
                    b_resamples=200,
                    eligible=APPENDIX_C,
                    exploratory=("k9",),
                    declaration=declared,
                )

    def test_a_declared_configuration_is_not_an_exploratory_variant(self) -> None:
        with pytest.raises(ValueError, match="already compared"):
            significance_families(
                {},
                headline="C01",
                salt="salt",
                b_resamples=10,
                eligible=APPENDIX_C,
                exploratory=("C05",),
                declaration=self._declared(),
            )

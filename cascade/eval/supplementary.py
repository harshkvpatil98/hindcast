"""Supplementary comparisons, declared in advance. Pure definitions.

Appendix C's twelve cells answer the questions the spec asked. A supplementary
cell answers one the study's owner asked afterwards -- and it is declared
*here, in code, before it is run*, because that is the only thing separating a
planned comparison from the best of several tried.

Three rules keep a supplementary cell from contaminating what it sits beside:

* **Its own Holm family.** Holm-Bonferroni multiplies the smallest p-value by
  the family size, so admitting S01 to the twelve-cell family would weaken
  every ablation result by a question Appendix C never asked.
  :func:`supplementary_family` is adjusted on its own, and
  ``ablation.comparison_family`` is told to leave these ids out. The cost is
  stated rather than hidden: the two families are each controlled at their own
  alpha, and the report says which family every row belongs to.
* **Never run by default.** ``cascade eval grid`` executes the twelve cells;
  ``--supplementary`` adds these. A study that did not ask for them does not
  pay for them and does not report them.
* **Declared is not adopted.** A supplementary delta on the test partition is a
  reportable finding. Moving the headline configuration *because of it* is
  tuning on test. That decision belongs to the dev partition, and the report
  repeats this beside the table.

The overlay files live in ``configs/supplementary/``, not ``configs/ablations/``
-- that directory holds exactly Appendix C's twelve and a test asserts it.
``tests/unit/test_eval_supplementary.py`` asserts each overlay differs from the
headline configuration in exactly the settings :data:`OVERRIDES` names.
"""

from __future__ import annotations

from collections.abc import Sequence

from cascade.eval.ablation import CELLS, HEADLINE_CELL, CellSpec, ComparisonSpec

__all__ = [
    "OVERRIDES",
    "SUPPLEMENTARY_CELLS",
    "supplementary_by_id",
    "supplementary_family",
    "supplementary_ids",
]

# A supplementary cell is a CellSpec so the grid driver runs it through exactly
# the code that runs the twelve: same replicate policy, same scenario
# subsample, same collapse. `replicates_design` is the headline's D because
# each of these *is* the headline configuration with one setting moved.
SUPPLEMENTARY_CELLS: tuple[CellSpec, ...] = (
    CellSpec(
        "S01",
        True,
        True,
        "chronofence",
        200,
        "Headline configuration with 12 evidence chunks per agent (headline: 6)",
    ),
)

# The one setting each cell moves, as (dotted setting, value). Two independent
# statements of the design -- this and the overlay file -- that a test requires
# to agree, as `ablation.CELLS` is checked against `configs/ablations/`.
OVERRIDES: dict[str, tuple[tuple[str, int], ...]] = {
    "S01": (("retrieval.k_agent", 12),),
}

_READINGS: dict[str, str] = {
    "S01": (
        "Brier(k_agent=12) - Brier(k_agent=6), everything else the headline "
        "configuration. Negative means twelve evidence chunks per agent forecast "
        "better than six. Under the budget-capped replicate policy S01 runs at the "
        "capped replicate count against the headline's full D, so -- like every "
        "capped cell's delta -- this one contains the ensemble-size difference; see "
        "the replicate-policy note. Supplementary: adjusted in its own Holm family, "
        "not Appendix C's."
    ),
}


def supplementary_ids() -> tuple[str, ...]:
    """Every supplementary cell id, sorted. Disjoint from Appendix C's twelve."""
    return tuple(sorted(cell.cell_id for cell in SUPPLEMENTARY_CELLS))


def supplementary_by_id(cell_id: str) -> CellSpec:
    """Look up a supplementary cell, or raise naming the ones that exist."""
    for cell in SUPPLEMENTARY_CELLS:
        if cell.cell_id == cell_id:
            return cell
    raise KeyError(
        f"{cell_id!r} is not a supplementary cell; declared: {list(supplementary_ids())}"
    )


def supplementary_family(
    *, available: Sequence[str], headline: str = HEADLINE_CELL
) -> tuple[ComparisonSpec, ...]:
    """The supplementary Holm family: each declared cell against the headline.

    Preserves the separation of families. Only declared supplementary cells
    appear, only when both sides have forecasts (an untestable hypothesis
    inflates the multiplier), and never a cell of the twelve -- so the same
    comparison cannot be adjusted twice under two different multipliers.
    """
    have = set(available)
    if headline not in have:
        return ()
    return tuple(
        ComparisonSpec(
            name=f"{cell.cell_id} vs {headline} (supplementary)",
            config_a=cell.cell_id,
            config_b=headline,
            reading=_READINGS[cell.cell_id],
        )
        for cell in SUPPLEMENTARY_CELLS
        if cell.cell_id in have and cell.cell_id != headline
    )


def _assert_disjoint() -> None:
    clash = {cell.cell_id for cell in CELLS} & set(supplementary_ids())
    if clash:  # pragma: no cover - a definition error, caught at import
        raise AssertionError(f"supplementary ids collide with Appendix C cells: {sorted(clash)}")


_assert_disjoint()

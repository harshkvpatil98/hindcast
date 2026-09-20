"""What the report *says*, as opposed to which files it writes.

Split from `test_eval_report.py` because these assert on the prose a reader
actually reads. §1 is a claim about the report's text as much as about its
arithmetic: a number that could not be produced must read as not produced, and
a number produced by a stand-in must not read like a result.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from cascade.eval.ablation import CELLS, grid_replicates, headline_comparisons
from cascade.eval.market import MARKET_CONFIG_ID, CoverageSummary
from cascade.eval.report import StudyArtifact, write_report
from cascade.eval.schema import (
    AblationCell,
    BootstrapInterval,
    Comparison,
    DispersionFinding,
    MetricSet,
    MurphyTerms,
)


def _artifact(**overrides: object) -> StudyArtifact:
    base: dict[str, object] = {
        "report_id": "study_20260811T1440Z",
        "written_at": datetime(2026, 8, 11, 14, 40, tzinfo=UTC),
        "manifest_sha256": "a" * 64,
        "git_sha": "abc123",
        "n_scenarios_sealed": 180,
        "base_rate": 0.5,
        "climatology_brier": 0.25,
        "study_salt": "salt",
        "models": {"agent": "haiku", "compiler": "sonnet"},
        "config_snapshot": {},
    }
    base.update(overrides)
    return StudyArtifact(**base)  # type: ignore[arg-type]


def _metrics(config_id: str, brier: float) -> MetricSet:
    return MetricSet(
        config_id=config_id,
        n=180,
        base_rate=0.5,
        mean_p_hat=0.5,
        brier=brier,
        log_loss=0.6,
        auc=0.6,
        ece=0.05,
        mce=0.1,
        murphy=MurphyTerms(
            reliability=0.01,
            resolution=0.02,
            uncertainty=0.25,
            brier=brier,
            residual=0.0,
            bins=10,
        ),
        bss_vs_climatology=1 - brier / 0.25,
    )


def _comparison(name: str, point: float) -> Comparison:
    return Comparison(
        name=name,
        config_a="C05",
        config_b="C01",
        brier_a=0.2,
        brier_b=0.2 - point,
        n_paired=90,
        interval=BootstrapInterval(
            point=point, lo=point - 0.01, hi=point + 0.01, b=10000, p_value=0.002
        ),
        p_adjusted=0.01,
    )


class TestTheDeltasAreReadCorrectly:
    def test_the_nesting_warning_is_in_the_report_not_only_in_the_code(
        self, tmp_path: Path
    ) -> None:
        """§10.3: "they do not sum, and the report must say so"."""
        directory = write_report(_artifact(), root=tmp_path)
        headline = (directory / "headline.md").read_text()
        assert "do not sum" in headline

    def test_every_delta_is_printed_with_the_count_it_was_paired_on(self, tmp_path: Path) -> None:
        """One table holds deltas over different populations -- 90 for a capped
        cell, fewer for the market benchmark -- so each row says its own n."""
        artifact = _artifact(comparisons=(_comparison("LOO information asymmetry", 0.02),))
        directory = write_report(artifact, root=tmp_path)
        headline = (directory / "headline.md").read_text()
        assert "| Comparison | delta Brier | 95% CI | n paired |" in headline
        row = next(
            line for line in headline.splitlines() if "| LOO information asymmetry |" in line
        )
        assert row.split("|")[4].strip() == "90"

    def test_every_comparison_carries_its_reading(self, tmp_path: Path) -> None:
        readings = {spec.name: spec.reading for spec in headline_comparisons()}
        artifact = _artifact(
            comparisons=(_comparison("LOO information asymmetry", 0.02),),
            comparison_readings=readings,
        )
        directory = write_report(artifact, root=tmp_path)
        headline = (directory / "headline.md").read_text()
        assert readings["LOO information asymmetry"][:40] in headline

    def test_the_reading_is_machine_readable_beside_the_number(self, tmp_path: Path) -> None:
        readings = {spec.name: spec.reading for spec in headline_comparisons()}
        artifact = _artifact(
            comparisons=(_comparison("LOO causal decomposition", 0.03),),
            comparison_readings=readings,
        )
        directory = write_report(artifact, root=tmp_path)
        payload = json.loads((directory / "significance.json").read_text())
        assert payload["comparisons"][0]["reading"]
        assert payload["method"]["resample_unit"] == "scenario"
        assert "Holm" in payload["method"]["multiple_comparison"]

    def test_no_comparisons_says_so_rather_than_printing_an_empty_table(
        self, tmp_path: Path
    ) -> None:
        directory = write_report(_artifact(), root=tmp_path)
        assert "no delta was computed" in (directory / "headline.md").read_text()


class TestTheReplicatePolicyIsStated:
    def test_the_report_names_which_reading_of_q1_it_applied(self, tmp_path: Path) -> None:
        """§10.3 warns the ensemble-contribution estimate depends on it."""
        notes = tuple(
            grid_replicates(cell, policy="budget_capped", ablation_cap=30)[1] for cell in CELLS
        )
        artifact = _artifact(
            replicate_policy="D is the design factor; 30 is a budget cap.",
            replicate_notes=notes,
        )
        headline = (write_report(artifact, root=tmp_path) / "headline.md").read_text()
        assert "## Replicate policy" in headline
        assert "budget cap" in headline
        assert "C09 has Appendix C design factor D=200" in headline

    def test_a_grid_that_never_ran_still_says_so(self, tmp_path: Path) -> None:
        headline = (write_report(_artifact(), root=tmp_path) / "headline.md").read_text()
        assert "no ablation cell was executed" in headline


class TestDispersionIsReportedHoweverItFell:
    def test_the_finding_is_printed_with_both_statistics(self, tmp_path: Path) -> None:
        finding = DispersionFinding(
            n=180,
            pearson_r=0.31,
            pearson_p=0.001,
            spearman_rho=0.28,
            spearman_p=0.002,
            flagged=56,
            brier_flagged=0.31,
            brier_unflagged=0.12,
            sigma_threshold=0.3,
        )
        headline = (
            write_report(_artifact(dispersion=finding), root=tmp_path) / "headline.md"
        ).read_text()
        assert "0.2800" in headline
        assert "56 of 180" in headline
        assert "whichever way they fell" in headline

    def test_an_unmeasurable_correlation_reads_as_not_measured(self, tmp_path: Path) -> None:
        finding = DispersionFinding(
            n=10,
            pearson_r=None,
            pearson_p=None,
            spearman_rho=None,
            spearman_p=None,
            flagged=0,
            brier_flagged=None,
            brier_unflagged=0.2,
            sigma_threshold=0.3,
        )
        headline = (
            write_report(_artifact(dispersion=finding), root=tmp_path) / "headline.md"
        ).read_text()
        assert "not measured" in headline


class TestCellsAreMachineReadable:
    def test_design_and_executed_replicate_counts_are_both_recorded(self, tmp_path: Path) -> None:
        """CLAUDE.md Q1 turns on these being different numbers; the report
        carries both and never reconciles them silently."""
        cells = (
            AblationCell(
                cell_id="C05",
                config_id="C05",
                decomposition=True,
                information_asymmetry=False,
                grounding="chronofence",
                replicates_design=200,
                replicates_executed=30,
                scenarios_executed=90,
                role="LOO information asymmetry",
                metrics=_metrics("C05", 0.168),
            ),
        )
        directory = write_report(_artifact(cells=cells), root=tmp_path)
        payload = json.loads((directory / "metrics.json").read_text())
        cell = payload["cells"][0]
        assert cell["replicates_design"] == 200
        assert cell["replicates_executed"] == 30
        assert cell["scenarios_executed"] == 90

    def test_a_cell_with_no_metrics_records_null_not_zero(self, tmp_path: Path) -> None:
        cells = (
            AblationCell(
                cell_id="C07",
                config_id="C07",
                decomposition=True,
                information_asymmetry=False,
                grounding="parametric_only",
                replicates_design=200,
                replicates_executed=None,
                scenarios_executed=0,
                role="Asymmetry x grounding interaction",
                metrics=None,
            ),
        )
        directory = write_report(_artifact(cells=cells), root=tmp_path)
        payload = json.loads((directory / "metrics.json").read_text())
        assert payload["cells"][0]["metrics"] is None

    def test_the_baseline_table_lists_all_five_even_when_absent(self, tmp_path: Path) -> None:
        """...and the market benchmark beside them, equally visible when absent:
        a report that dropped the row would hide that nobody fetched a price."""
        from cascade.eval.baselines import BASELINES

        baselines = tuple((spec.baseline_id, spec.name, spec.config_id, None) for spec in BASELINES)
        directory = write_report(_artifact(baselines=baselines), root=tmp_path)
        payload = json.loads((directory / "metrics.json").read_text())
        in_spec = {spec.baseline_id for spec in BASELINES if spec.in_spec}
        assert len([row for row in payload["baselines"] if row["baseline_id"] in in_spec]) == 5
        assert "market" in {row["baseline_id"] for row in payload["baselines"]}
        assert all(row["metrics"] is None for row in payload["baselines"])


def _coverage(*, n_scenarios: int = 180, usable: int = 143, stale: int = 6) -> CoverageSummary:
    """A coverage summary whose parts add up to the sealed set, as the real one does."""
    return CoverageSummary(
        n_scenarios=n_scenarios,
        n_priced=usable + stale,
        n_usable=usable,
        n_stale=stale,
        max_staleness_seconds=86_400.0,
        unobtainable=(
            ("market_created_after_cutoff", 1),
            ("no_price_history", 21),
            ("not_a_market", 9),
        ),
        by_source=(("curated", 9, 0, 0), ("manifold", 6, 0, 6), ("polymarket", 165, usable, 0)),
        staleness=None,
    )


def _market_metrics(n: int, brier: float) -> MetricSet:
    return _metrics(MARKET_CONFIG_ID, brier).model_copy(
        update={"n": n, "policies": ("none",), "base_rate": 0.5514}
    )


class TestTheBaselinesAreInTheProse:
    """§10.2 asks for five baselines "in the report". `metrics.json` is a file a
    script reads; `headline.md` is the file a reader reads, and until it carried
    the table the market's Brier existed only as a delta."""

    def test_every_baseline_is_a_row_with_its_own_n(self, tmp_path: Path) -> None:
        baselines = (
            ("climatology", "Climatology", "B1_climatology", _metrics("B1_climatology", 0.25)),
            ("market", "Market at the cutoff", MARKET_CONFIG_ID, _market_metrics(107, 0.2078)),
        )
        artifact = _artifact(baselines=baselines, partition="test")
        headline = (write_report(artifact, root=tmp_path) / "headline.md").read_text()
        assert "## Baselines" in headline
        market_row = next(
            line for line in headline.splitlines() if line.startswith("| Market at the cutoff |")
        )
        assert market_row.split("|")[3].strip() == "107"
        assert "0.207800" in market_row

    def test_a_baseline_with_no_forecasts_reads_as_not_produced(self, tmp_path: Path) -> None:
        baselines = (("climatology", "Climatology", "B1_climatology", None),)
        headline = (
            write_report(_artifact(baselines=baselines), root=tmp_path) / "headline.md"
        ).read_text()
        row = next(line for line in headline.splitlines() if line.startswith("| Climatology |"))
        assert "not produced" in row
        assert "0.0000" not in row

    def test_the_rows_are_said_not_to_be_paired(self, tmp_path: Path) -> None:
        """Two rows over different scenario sets sit in one table; subtracting
        them is the mistake the table has to forbid in words."""
        baselines = (
            ("market", "Market at the cutoff", MARKET_CONFIG_ID, _market_metrics(107, 0.2)),
        )
        headline = (
            write_report(_artifact(baselines=baselines), root=tmp_path) / "headline.md"
        ).read_text()
        assert "not paired" in headline
        assert "must not be subtracted" in headline


class TestTheMarketCoverageTravelsWithItsBrier:
    def test_the_counts_are_printed_where_the_brier_is(self, tmp_path: Path) -> None:
        baselines = (
            ("market", "Market at the cutoff", MARKET_CONFIG_ID, _market_metrics(107, 0.2)),
        )
        artifact = _artifact(baselines=baselines, market_coverage=_coverage(), partition="test")
        headline = (write_report(artifact, root=tmp_path) / "headline.md").read_text()
        assert "**143 usable** of 180 sealed scenarios" in headline
        assert "6 priced but **stale**" in headline
        assert "21 **unobtainable: no_price_history**" in headline
        assert "9 **unobtainable: not_a_market**" in headline
        assert "1 **unobtainable: market_created_after_cutoff**" in headline

    def test_the_partition_n_is_reconciled_against_the_usable_count(self, tmp_path: Path) -> None:
        """143 usable, 107 scored here: three different denominators are in play
        and the report says which one the Brier used."""
        baselines = (
            ("market", "Market at the cutoff", MARKET_CONFIG_ID, _market_metrics(107, 0.2)),
        )
        artifact = _artifact(baselines=baselines, market_coverage=_coverage(), partition="test")
        headline = (write_report(artifact, root=tmp_path) / "headline.md").read_text()
        assert "Of those usable prices, **107** fall on scenarios" in headline
        assert "not the usable count" in headline

    def test_the_selection_is_named_as_a_selection(self, tmp_path: Path) -> None:
        baselines = (
            ("market", "Market at the cutoff", MARKET_CONFIG_ID, _market_metrics(107, 0.2)),
        )
        artifact = _artifact(baselines=baselines, market_coverage=_coverage(), base_rate=0.5)
        headline = (write_report(artifact, root=tmp_path) / "headline.md").read_text()
        assert "not a random sample" in headline
        assert "0.5514" in headline

    def test_unmeasured_coverage_says_so_rather_than_being_absent(self, tmp_path: Path) -> None:
        baselines = (
            ("market", "Market at the cutoff", MARKET_CONFIG_ID, _market_metrics(107, 0.2)),
        )
        artifact = _artifact(baselines=baselines, market_coverage=None)
        headline = (write_report(artifact, root=tmp_path) / "headline.md").read_text()
        assert "**Coverage was not measured.**" in headline

    def test_the_coverage_is_inside_the_market_s_own_metric_block(self, tmp_path: Path) -> None:
        """A script that reads only `metrics.json` must not be able to take the
        market's Brier without its denominator."""
        metrics = (_market_metrics(107, 0.2078), _metrics("C09", 0.2479))
        artifact = _artifact(metrics=metrics, market_coverage=_coverage())
        payload = json.loads((write_report(artifact, root=tmp_path) / "metrics.json").read_text())
        blocks = {row["config_id"]: row for row in payload["configs"]}
        assert blocks[MARKET_CONFIG_ID]["market_coverage"]["n_usable"] == 143
        assert blocks[MARKET_CONFIG_ID]["market_coverage"]["measured"] is True
        assert "market_coverage" not in blocks["C09"]

    def test_an_unmeasured_coverage_is_a_stated_absence_in_the_json_too(
        self, tmp_path: Path
    ) -> None:
        artifact = _artifact(metrics=(_market_metrics(107, 0.2078),), market_coverage=None)
        payload = json.loads((write_report(artifact, root=tmp_path) / "metrics.json").read_text())
        coverage = payload["configs"][0]["market_coverage"]
        assert coverage["measured"] is False
        assert "market-prices" in coverage["note"]


class TestStandInForecastsAreFlaggedAboveTheNumbers:
    def test_every_stand_in_configuration_is_named_before_any_section(self, tmp_path: Path) -> None:
        """The headline banner covers only the lead figure. A report whose
        headline has no forecasts still ships a grid CSV and figures built
        entirely from stand-in runs."""
        metrics = (
            _metrics("C09", 0.2479).model_copy(update={"policies": ("heuristic",), "n": 30}),
            _metrics("C10", 0.2395).model_copy(update={"policies": ("heuristic",), "n": 30}),
        )
        artifact = _artifact(metrics=metrics, headline_config="C01", blocked=("no C01.",))
        headline = (write_report(artifact, root=tmp_path) / "headline.md").read_text()
        assert "stand-in decider" in headline
        assert "`C09` (heuristic, n=30)" in headline
        assert "`C10` (heuristic, n=30)" in headline
        assert headline.index("stand-in decider") < headline.index("## Not produced")

    def test_an_all_agent_artifact_carries_no_banner(self, tmp_path: Path) -> None:
        artifact = _artifact(metrics=(_metrics("C01", 0.2),), headline_config="C01")
        headline = (write_report(artifact, root=tmp_path) / "headline.md").read_text()
        assert "stand-in decider" not in headline


class TestCellsAsExecutedBridgeStoredToScored:
    def test_both_counts_are_printed_with_the_partition_named(self, tmp_path: Path) -> None:
        """40 forecasts stored, 30 scored on test: the gap is the excluded
        placeholder legs and the other partition, and a reader cannot
        reconstruct it from either number alone."""
        cells = (
            AblationCell(
                cell_id="C09",
                config_id="C09",
                decomposition=False,
                information_asymmetry=False,
                grounding="chronofence",
                replicates_design=200,
                replicates_executed=30,
                scenarios_executed=40,
                role="LOO decomposition",
                metrics=_metrics("C09", 0.2479).model_copy(update={"n": 30}),
            ),
        )
        headline = (
            write_report(_artifact(cells=cells, partition="test"), root=tmp_path) / "headline.md"
        ).read_text()
        assert "## Cells as executed" in headline
        row = next(line for line in headline.splitlines() if line.startswith("| C09 |"))
        assert row.split("|")[4].strip() == "40"
        assert row.split("|")[5].strip() == "30"
        assert "scored on test" in headline

    def test_a_grid_that_never_ran_has_no_such_table(self, tmp_path: Path) -> None:
        headline = (write_report(_artifact(), root=tmp_path) / "headline.md").read_text()
        assert "## Cells as executed" not in headline


class TestTheDeltasNameWhatTheyAreAgainst:
    def test_the_headline_configuration_is_named(self, tmp_path: Path) -> None:
        headline = (
            write_report(_artifact(headline_config="C01"), root=tmp_path) / "headline.md"
        ).read_text()
        assert "against `C01`, the configuration this report leads with" in headline

    def test_a_headline_that_is_not_the_full_configuration_says_so(self, tmp_path: Path) -> None:
        """Leading with C09 and calling its deltas leave-one-out would describe
        §6.4's design while measuring something else."""
        headline = (
            write_report(_artifact(headline_config="C09"), root=tmp_path) / "headline.md"
        ).read_text()
        assert "is **not** Appendix C's full configuration" in headline
        assert "not the leave-one-out deltas" in headline

    def test_the_decider_column_has_a_legend(self, tmp_path: Path) -> None:
        """`none` in a decider column reads as a missing value; here it means
        no decider of this study's produced the number at all."""
        baselines = (
            ("market", "Market at the cutoff", MARKET_CONFIG_ID, _market_metrics(107, 0.2)),
        )
        headline = (
            write_report(_artifact(baselines=baselines), root=tmp_path) / "headline.md"
        ).read_text()
        assert "`none` means no decider of this study's produced the number" in headline

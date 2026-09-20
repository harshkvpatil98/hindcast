"""The Appendix D artifact, and invariant §14.3.

"The report prints measured values. No target value is ever written into a
report path." That is stated as a static check over the package rather than as
a promise, because the failure it prevents is one nobody reviewing a diff would
see: a constant that happens to equal the number the study hopes for.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cascade.eval.report import StudyArtifact, report_id, write_report
from cascade.eval.schema import (
    CalibrationBin,
    CalibrationReport,
    DispersionFinding,
    MetricSet,
    MurphyTerms,
    ScoredForecast,
)

# Every headline figure from CLAUDE.md §1's measurement contract. None of these
# may appear as a literal anywhere on a report code path.
TARGET_LITERALS = (
    0.141,
    0.203,
    0.176,
    0.168,
    0.035,
    0.027,
    0.008,
    0.305,
    0.199,
    0.347,
    0.31,
    0.0035,
)

REPORT_PATH_MODULES = (
    "cascade/eval/report.py",
    "cascade/eval/metrics.py",
    "cascade/eval/stats.py",
    "cascade/eval/score.py",
    "cascade/eval/ablation.py",
    "cascade/eval/figures.py",
    # Decides which market prices are scored, so it is on the path to a
    # reported Brier like everything above.
    "cascade/eval/market.py",
    "cascade/eval/split.py",
    "cascade/eval/evidence.py",
    "cascade/eval/supplementary.py",
)


def _metrics(config_id: str = "C01") -> MetricSet:
    return MetricSet(
        config_id=config_id,
        n=12,
        base_rate=0.5,
        mean_p_hat=0.48,
        brier=0.2201,
        log_loss=0.61,
        auc=0.62,
        ece=0.07,
        mce=0.19,
        murphy=MurphyTerms(
            reliability=0.01,
            resolution=0.04,
            uncertainty=0.25,
            brier=0.2201,
            residual=0.0001,
            bins=10,
        ),
    )


def _scored(count: int = 6) -> tuple[ScoredForecast, ...]:
    return tuple(
        ScoredForecast(
            scenario_id=f"s{index}",
            config_id="C01",
            p_hat=0.1 + 0.15 * index,
            outcome=index % 2,
            domain="elections" if index % 2 else "macro_policy",
            sigma=0.05 * index,
        )
        for index in range(count)
    )


def _dispersion() -> DispersionFinding:
    return DispersionFinding(
        n=6,
        pearson_r=0.2,
        pearson_p=0.4,
        spearman_rho=0.21,
        spearman_p=0.39,
        flagged=0,
        brier_flagged=None,
        brier_unflagged=0.22,
        sigma_threshold=0.3,
    )


def _artifact(tmp_path: Path, **overrides: object) -> StudyArtifact:
    base = {
        "report_id": report_id(now=datetime(2026, 8, 11, 14, 40, tzinfo=UTC)),
        "written_at": datetime(2026, 8, 11, 14, 40, tzinfo=UTC),
        "manifest_sha256": "a" * 64,
        "git_sha": "deadbeef",
        "n_scenarios_sealed": 180,
        "base_rate": 0.5,
        "climatology_brier": 0.25,
        "study_salt": "salt",
        "models": {"agent": "haiku", "compiler": "sonnet"},
        "config_snapshot": {"kernel": {"steps": 24}},
    }
    base.update(overrides)
    return StudyArtifact(**base)  # type: ignore[arg-type]


class TestNoTargetInAReportPath:
    """Invariant §14.3, enforced by a test rather than by intention."""

    @pytest.mark.parametrize("module", REPORT_PATH_MODULES)
    def test_no_headline_target_appears_as_a_numeric_literal(self, module: str) -> None:
        from cascade.config import repo_root

        source = (repo_root() / module).read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, float)
            and any(abs(node.value - target) < 1e-12 for target in TARGET_LITERALS)
        ]
        assert found == [], (
            f"{module} contains {found}, which are values from the measurement "
            "contract. A report path must compute what it prints (§1, §14.3)."
        )

    def test_the_check_would_catch_a_real_violation(self) -> None:
        """A guard that cannot fail is not a guard."""
        tree = ast.parse("BRIER_TARGET = 0.141\n")
        found = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, float)
            and any(abs(node.value - target) < 1e-12 for target in TARGET_LITERALS)
        ]
        assert found == [0.141]


class TestArtifactStructure:
    def test_it_writes_every_file_appendix_d_names(self, tmp_path: Path) -> None:
        directory = write_report(_artifact(tmp_path), root=tmp_path)
        expected = {
            "manifest.json",
            "headline.md",
            "metrics.json",
            "baselines.csv",
            "ablation_grid.csv",
            "calibration.csv",
            "per_domain.csv",
            "significance.json",
            "leakage_report.json",
            "cost_ledger.json",
        }
        assert expected <= {path.name for path in directory.iterdir()}

    def test_the_directory_name_is_the_appendix_d_shape(self) -> None:
        assert report_id(now=datetime(2026, 8, 11, 14, 40, tzinfo=UTC)) == "study_20260811T1440Z"

    def test_the_timestamp_is_an_argument_not_a_clock_read(self) -> None:
        """Invariant 1's reasoning applied to the report: a test that cannot
        fix the timestamp cannot assert on the path."""
        first = report_id(now=datetime(2026, 1, 1, tzinfo=UTC))
        second = report_id(now=datetime(2026, 1, 1, tzinfo=UTC))
        assert first == second

    def test_the_manifest_records_the_sealed_split_and_the_commit(self, tmp_path: Path) -> None:
        directory = write_report(_artifact(tmp_path), root=tmp_path)
        payload = json.loads((directory / "manifest.json").read_text())
        assert payload["manifest_sha256"] == "a" * 64
        assert payload["git_sha"] == "deadbeef"
        assert payload["sealed_set"]["n_scenarios"] == 180

    def test_the_manifest_records_the_figure_format_deviation(self, tmp_path: Path) -> None:
        """Appendix D says .png; ADR-0024 says why they are .svg. A reader
        should not have to infer it from a directory listing."""
        directory = write_report(_artifact(tmp_path), root=tmp_path)
        payload = json.loads((directory / "manifest.json").read_text())
        assert payload["figure_format"] == "svg"
        assert "ADR-0024" in payload["figure_format_note"]

    def test_json_is_written_with_sorted_keys(self, tmp_path: Path) -> None:
        """The artifact is committed; a re-run must diff on numbers, not keys."""
        directory = write_report(_artifact(tmp_path), root=tmp_path)
        text = (directory / "metrics.json").read_text()
        assert json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n" == text

    def test_rewriting_the_same_id_is_idempotent(self, tmp_path: Path) -> None:
        first = write_report(_artifact(tmp_path), root=tmp_path)
        second = write_report(_artifact(tmp_path), root=tmp_path)
        assert first == second


class TestBlockedQuantities:
    def test_a_blocked_study_says_so_in_its_first_section(self, tmp_path: Path) -> None:
        artifact = _artifact(tmp_path, blocked=("Headline Brier: no forecasts.",))
        directory = write_report(artifact, root=tmp_path)
        headline = (directory / "headline.md").read_text()
        assert "## Not produced" in headline
        assert headline.index("## Not produced") < headline.index("## Headline")

    def test_a_missing_headline_is_stated_not_dashed(self, tmp_path: Path) -> None:
        directory = write_report(_artifact(tmp_path), root=tmp_path)
        headline = (directory / "headline.md").read_text()
        assert "has no scoreable forecasts" in headline

    def test_a_missing_metric_reads_as_not_measured(self, tmp_path: Path) -> None:
        artifact = _artifact(tmp_path, metrics=(_metrics(),), headline_config="C01")
        directory = write_report(artifact, root=tmp_path)
        assert "not measured" in (directory / "headline.md").read_text()

    def test_not_produced_is_machine_readable_too(self, tmp_path: Path) -> None:
        artifact = _artifact(tmp_path, blocked=("A quantity.",))
        directory = write_report(artifact, root=tmp_path)
        payload = json.loads((directory / "manifest.json").read_text())
        assert payload["not_produced"] == ["A quantity."]


class TestFigures:
    def test_a_figure_with_no_data_is_not_written(self, tmp_path: Path) -> None:
        """An empty axis reads as "we looked and found nothing" when the truth
        is that the measurement did not happen."""
        directory = write_report(_artifact(tmp_path), root=tmp_path)
        assert list((directory / "figures").iterdir()) == []

    def test_the_reliability_diagram_is_written_when_there_is_calibration(
        self, tmp_path: Path
    ) -> None:
        report = CalibrationReport(
            bins=(
                CalibrationBin(
                    index=4,
                    lo=0.4,
                    hi=0.5,
                    count=3,
                    mean_pred=0.45,
                    obs_freq=0.33,
                    wilson_lo=0.1,
                    wilson_hi=0.7,
                ),
            ),
            ece=0.12,
            mce=0.12,
            n=3,
        )
        directory = write_report(_artifact(tmp_path, calibration=report), root=tmp_path)
        svg = (directory / "figures" / "reliability.svg").read_text()
        assert svg.startswith("<svg")
        assert "ECE 0.1200" in svg


class TestPolicyIsSurfaced:
    def test_a_heuristic_headline_is_flagged_before_its_numbers(self, tmp_path: Path) -> None:
        """M5 stamps the policy on a run because a footnote is not a mechanism.
        The same holds for a report that leads with a Brier."""
        metrics = _metrics().model_copy(update={"policies": ("heuristic",)})
        artifact = _artifact(tmp_path, metrics=(metrics,), headline_config="C01")
        headline = (write_report(artifact, root=tmp_path) / "headline.md").read_text()
        assert "not the study's agents" in headline
        assert headline.index("not the study's agents") < headline.index("**Brier")

    def test_an_agent_headline_carries_no_warning(self, tmp_path: Path) -> None:
        artifact = _artifact(tmp_path, metrics=(_metrics(),), headline_config="C01")
        headline = (write_report(artifact, root=tmp_path) / "headline.md").read_text()
        assert "not the study's agents" not in headline


class TestCsvRows:
    def test_the_grid_csv_carries_one_row_per_scenario_per_cell(self, tmp_path: Path) -> None:
        rows = tuple(
            (item.config_id, item.scenario_id, item.p_hat, item.sigma, item.outcome)
            for item in _scored()
        )
        directory = write_report(_artifact(tmp_path, grid_rows=rows), root=tmp_path)
        lines = (directory / "ablation_grid.csv").read_text().strip().splitlines()
        assert lines[0] == "cell_id,scenario_id,p_hat,sigma,outcome"
        assert len(lines) == len(rows) + 1

    def test_an_empty_calibration_still_writes_its_header(self, tmp_path: Path) -> None:
        directory = write_report(_artifact(tmp_path), root=tmp_path)
        text = (directory / "calibration.csv").read_text()
        assert text.strip() == "bin,lo,hi,count,mean_pred,obs_freq,wilson_lo,wilson_hi"


class TestTheIndexDescribesTheDirectoryThatExists:
    """The Artifact table is the report's one promise about itself. It named ten
    files and four figures unconditionally, so a run that wrote a header-only
    CSV and no figure still published a table claiming otherwise."""

    def test_every_figure_the_index_calls_written_is_on_disk(self, tmp_path: Path) -> None:
        directory = write_report(
            _artifact(tmp_path, scored=_scored(), dispersion=_dispersion()), root=tmp_path
        )
        headline = (directory / "headline.md").read_text()
        present = {path.name for path in (directory / "figures").iterdir()}
        for line in headline.splitlines():
            if line.startswith("| `") and ".svg`" in line:
                name = line.split("`")[1]
                written = "**no**" not in line
                assert (name in present) is written, line

    def test_a_figure_that_was_not_drawn_carries_its_reason(self, tmp_path: Path) -> None:
        headline = (write_report(_artifact(tmp_path), root=tmp_path) / "headline.md").read_text()
        row = next(line for line in headline.splitlines() if "`convergence.svg`" in line)
        assert "**no**" in row
        assert "first rung" in row

    def test_an_empty_csv_reads_as_a_reason_not_as_a_zero(self, tmp_path: Path) -> None:
        headline = (write_report(_artifact(tmp_path), root=tmp_path) / "headline.md").read_text()
        row = next(line for line in headline.splitlines() if "`per_domain.csv`" in line)
        assert "0 — the headline configuration has no forecasts" in row

    def test_a_populated_csv_states_its_own_row_count(self, tmp_path: Path) -> None:
        rows = tuple(
            (item.config_id, item.scenario_id, item.p_hat, item.sigma, item.outcome)
            for item in _scored()
        )
        headline = (
            write_report(_artifact(tmp_path, grid_rows=rows), root=tmp_path) / "headline.md"
        ).read_text()
        row = next(line for line in headline.splitlines() if "`ablation_grid.csv`" in line)
        assert row.split("|")[2].strip() == str(len(rows))

    def test_the_figure_inventory_is_machine_readable(self, tmp_path: Path) -> None:
        directory = write_report(_artifact(tmp_path), root=tmp_path)
        payload = json.loads((directory / "manifest.json").read_text())
        inventory = {row["filename"]: row for row in payload["figures"]}
        assert set(inventory) == {
            "reliability.svg",
            "ablation_forest.svg",
            "convergence.svg",
            "sigma_vs_error.svg",
        }
        assert all(row["written"] is False for row in inventory.values())
        assert all(row["note"] for row in inventory.values())


class TestDegenerateFiguresAreDeclined:
    def test_one_convergence_rung_is_not_a_curve(self, tmp_path: Path) -> None:
        """A single rung has nothing to converge across, and the y axis would
        carry a maximum no datum reaches."""
        directory = write_report(
            _artifact(tmp_path, convergence=((25, 0.0, 0.0, 30),)), root=tmp_path
        )
        assert not (directory / "figures" / "convergence.svg").exists()
        row = next(
            line
            for line in (directory / "headline.md").read_text().splitlines()
            if "`convergence.svg`" in line
        )
        assert "one rung (25 replicates)" in row

    def test_two_rungs_are(self, tmp_path: Path) -> None:
        directory = write_report(
            _artifact(tmp_path, convergence=((25, 0.04, 0.1, 30), (50, 0.02, 0.1, 30))),
            root=tmp_path,
        )
        assert (directory / "figures" / "convergence.svg").exists()

    def test_a_sigma_column_with_no_spread_is_not_plotted(self, tmp_path: Path) -> None:
        """Every sigma identical -- what a D=1 cell produces -- draws 30 points
        on the left edge and reads as a scatter."""
        flat = tuple(item.model_copy(update={"sigma": 0.0}) for item in _scored())
        directory = write_report(
            _artifact(tmp_path, scored=flat, dispersion=_dispersion()), root=tmp_path
        )
        assert not (directory / "figures" / "sigma_vs_error.svg").exists()
        row = next(
            line
            for line in (directory / "headline.md").read_text().splitlines()
            if "`sigma_vs_error.svg`" in line
        )
        assert "no spread" in row

    def test_a_sigma_column_with_spread_is_plotted(self, tmp_path: Path) -> None:
        directory = write_report(
            _artifact(tmp_path, scored=_scored(), dispersion=_dispersion()), root=tmp_path
        )
        assert (directory / "figures" / "sigma_vs_error.svg").exists()


class TestWithheldAndProvenanceAreRecorded:
    def test_metrics_json_says_what_was_withheld_and_under_which_rule(self, tmp_path: Path) -> None:
        artifact = _artifact(tmp_path, withheld_configs=("V01",), partition="test")
        payload = json.loads((write_report(artifact, root=tmp_path) / "metrics.json").read_text())
        assert payload["withheld"]["configs"] == ["V01"]
        assert payload["withheld"]["partition"] == "test"
        assert "tuning variant" in payload["withheld"]["rule"]

    def test_the_manifest_records_what_the_forecasts_were_made_against(
        self, tmp_path: Path
    ) -> None:
        """The same configuration over a corpus half the size is a different
        experiment, and the size is not recoverable afterwards."""
        provenance = {
            "corpus": {"measured": True, "n_chunks": 1_998_127},
            "retrieval": {"mode": "hybrid"},
            "llm": {"prompt_rev": "r4"},
            "tools": {"python": "3.12.9"},
        }
        artifact = _artifact(tmp_path, provenance=provenance)
        payload = json.loads((write_report(artifact, root=tmp_path) / "manifest.json").read_text())
        assert payload["provenance"]["corpus"]["n_chunks"] == 1_998_127
        assert payload["provenance"]["retrieval"]["mode"] == "hybrid"
        assert payload["provenance"]["llm"]["prompt_rev"] == "r4"
        assert payload["provenance"]["tools"]["python"] == "3.12.9"

    def test_no_provenance_is_an_empty_object_not_an_invented_one(self, tmp_path: Path) -> None:
        payload = json.loads(
            (write_report(_artifact(tmp_path), root=tmp_path) / "manifest.json").read_text()
        )
        assert payload["provenance"] == {}

"""The report's headline is the test partition -- in arithmetic and in prose.

Two halves. `score.headline_by_partition` has to make the test figure a
function of test forecasts and test labels *alone*, recalibration included; and
`headline.md` has to say which figure is which, so the all-scenario number a
reader will go looking for is found already labelled "not the headline".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cascade.config import load_settings, repo_root
from cascade.eval.evidence import evidence_finding
from cascade.eval.exclusions import exclusions
from cascade.eval.metrics import brier
from cascade.eval.report import StudyArtifact, write_report
from cascade.eval.schema import BootstrapInterval, Comparison, ScoredForecast
from cascade.eval.score import (
    climatology_reference,
    headline_by_partition,
    measure,
    recalibrated_brier,
    split_halves,
)
from cascade.eval.split import PartitionInteraction, declare_study_split, select

# The sealed registry's (id, domain, question) -- no outcome. The question is
# there because the study split excludes exchange stand-ins by their wording
# before drawing the partition (ADR-0043).
STUDY: list[tuple[str, str, str]] = [
    (scenario_id, domain, question)
    for scenario_id, domain, question in json.loads(
        (repo_root() / "tests" / "fixtures" / "registry_domains.json").read_text(encoding="utf-8")
    )
]
REGISTRY: list[tuple[str, str]] = [(scenario_id, domain) for scenario_id, domain, _ in STUDY]
EXCLUDED = {item.scenario_id for item in exclusions([(i, q) for i, _, q in STUDY])}


def _declared():
    return declare_study_split(STUDY, salt=load_settings().study.salt, dev_size=40)


def _scored(*, dev_p: float = 0.35, config_id: str = "C01") -> tuple[ScoredForecast, ...]:
    """Every sealed scenario, with a forecast that depends on its side.

    Test forecasts are a fixed spread. Dev forecasts are whatever ``dev_p``
    says, so a test can make the dev partition arbitrarily good or bad and
    watch what the headline does.
    """
    dev = set(_declared().dev)
    return tuple(
        ScoredForecast(
            scenario_id=scenario_id,
            config_id=config_id,
            p_hat=dev_p if scenario_id in dev else ((index * 37) % 97) / 100,
            outcome=index % 2,  # type: ignore[arg-type]
            domain=domain,
        )
        for index, (scenario_id, domain) in enumerate(REGISTRY)
    )


class TestTheTestFigureIsAFunctionOfTestAlone:
    def test_no_dev_forecast_however_tuned_can_move_the_test_row(self) -> None:
        """Every metric, recalibration included, bit for bit."""
        salt = load_settings().study.salt
        rows = [
            dict(
                headline_by_partition(
                    _scored(dev_p=dev_p), _declared(), config_id="C01", base_rate=0.5, salt=salt
                )
            )
            for dev_p in (0.0, 0.35, 1.0)
        ]
        assert rows[0]["test"] == rows[1]["test"] == rows[2]["test"]
        assert rows[0]["test"].brier_recalibrated is not None
        assert rows[0]["dev"] != rows[2]["dev"]
        assert rows[0]["all"] != rows[2]["all"]

    def test_the_test_row_is_the_brier_of_the_test_scenarios(self) -> None:
        declared = _declared()
        scored = _scored(dev_p=1.0)
        test_rows = [item for item in scored if item.scenario_id in set(declared.test)]
        measured = dict(headline_by_partition(scored, declared, config_id="C01"))
        assert measured["test"].n == len(declared.test)
        assert measured["test"].brier == brier(
            [item.p_hat for item in test_rows], [item.outcome for item in test_rows]
        )
        # "all" is every *scored* scenario: the stand-ins are on neither side.
        assert measured["all"].n == 180 - len(EXCLUDED) and measured["dev"].n == 40
        assert not {item.scenario_id for item in select(scored, declared, "all")} & EXCLUDED
        assert measured["all"].brier != measured["test"].brier

    def test_the_order_is_test_then_all_then_dev(self) -> None:
        order = [name for name, _ in headline_by_partition(_scored(), _declared(), config_id="C01")]
        assert order == ["test", "all", "dev"]

    def test_a_partition_with_no_forecasts_is_not_measured(self) -> None:
        declared = _declared()
        dev_only = [item for item in _scored() if item.scenario_id in set(declared.dev)]
        measured = dict(headline_by_partition(dev_only, declared, config_id="C01"))
        assert measured["test"] is None
        assert measured["dev"].n == 40


class TestRecalibrationStaysInsideItsPartition:
    def test_both_halves_come_from_the_partition_being_measured(self) -> None:
        salt = load_settings().study.salt
        declared = _declared()
        test_rows = select(_scored(), declared, "test")
        fit, held = split_halves(test_rows, salt=salt)
        assert {item.scenario_id for item in (*fit, *held)} == set(declared.test)
        assert len(fit) + len(held) == len(declared.test) and fit and held

    def test_the_test_figure_is_fitted_on_test_scenarios_only(self) -> None:
        """Change every dev forecast and every dev label; the recalibrated test
        Brier must not move. If the fit reached across, it would."""
        salt = load_settings().study.salt
        declared = _declared()
        dev = set(declared.dev)
        flipped = tuple(
            (
                item.model_copy(update={"outcome": 1 - item.outcome, "p_hat": 1.0 - item.p_hat})
                if item.scenario_id in dev
                else item
            )
            for item in _scored()
        )
        original = recalibrated_brier(select(_scored(), declared, "test"), salt=salt)
        after = recalibrated_brier(select(flipped, declared, "test"), salt=salt)
        assert original is not None and original == after
        assert recalibrated_brier(_scored(), salt=salt) != recalibrated_brier(flipped, salt=salt)


class TestReferencesArePairedOnThePartition:
    def test_climatology_is_the_sealed_forecast_scored_on_these_scenarios(self) -> None:
        rows = select(_scored(), _declared(), "test")
        assert climatology_reference(rows, base_rate=0.5) == 0.25
        lopsided = [item.model_copy(update={"outcome": 1}) for item in rows]
        assert climatology_reference(lopsided, base_rate=0.4) == pytest.approx(0.36)

    def test_the_direct_reference_uses_only_the_scenarios_being_measured(self) -> None:
        declared = _declared()
        direct = _scored(dev_p=0.0, config_id="B2_single_direct")
        worse_on_dev = _scored(dev_p=1.0, config_id="B2_single_direct")
        rows = select(_scored(), declared, "test")
        assert measure(rows, config_id="C01", base_rate=0.5, direct=direct) == measure(
            rows, config_id="C01", base_rate=0.5, direct=worse_on_dev
        )

    def test_an_empty_set_is_not_measured(self) -> None:
        assert measure((), config_id="C01", base_rate=0.5) is None


# ---------------------------------------------------------------------------
# headline.md
# ---------------------------------------------------------------------------


def _artifact(partition: str = "test", **overrides: object) -> StudyArtifact:
    salt = load_settings().study.salt
    declared = _declared()
    scored = _scored(dev_p=1.0)
    by_partition = headline_by_partition(
        scored, declared, config_id="C01", base_rate=0.5, salt=salt
    )
    chosen = dict(by_partition)[partition]  # type: ignore[index]
    base: dict[str, object] = {
        "report_id": "study_20260919T1200Z",
        "written_at": datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
        "manifest_sha256": "a" * 64,
        "git_sha": "abc123",
        "n_scenarios_sealed": 180,
        "base_rate": 0.5,
        "climatology_brier": 0.25,
        "study_salt": salt,
        "models": {"agent": "haiku", "compiler": "sonnet"},
        "config_snapshot": {},
        "metrics": (chosen,),
        "split": declared,
        "partition": partition,
        "headline_partitions": by_partition,
        "split_interactions": (
            PartitionInteraction(
                partition="dev",
                n=40,
                ablation_subsample=16,
                recalibration_fit=18,
                recalibration_held=22,
            ),
            PartitionInteraction(
                partition="test",
                n=140,
                ablation_subsample=74,
                recalibration_fit=75,
                recalibration_held=65,
            ),
        ),
    }
    base.update(overrides)
    return StudyArtifact(**base)  # type: ignore[arg-type]


def _headline(tmp_path: Path, artifact: StudyArtifact) -> str:
    return (write_report(artifact, root=tmp_path) / "headline.md").read_text(encoding="utf-8")


class TestTheHeadlineIsTheTestPartition:
    def test_the_lead_figure_is_the_test_brier_and_says_so(self, tmp_path: Path) -> None:
        artifact = _artifact()
        measured = dict(artifact.headline_partitions)
        text = _headline(tmp_path, artifact)
        section = text[text.index("## Headline") :]
        lead = section[section.index("**Brier") : section.index("\n", section.index("**Brier"))]
        assert f"{measured['test'].brier:.6f}" in lead
        assert f"{measured['test'].n} scenarios" in lead and "test partition" in lead
        assert f"{measured['all'].brier:.6f}" not in lead

    def test_the_all_scenario_figure_is_beside_it_and_labelled(self, tmp_path: Path) -> None:
        artifact = _artifact()
        measured = dict(artifact.headline_partitions)
        text = _headline(tmp_path, artifact)
        section = text[text.index("## Headline") :]
        row = next(line for line in section.splitlines() if line.startswith("| all |"))
        assert f"| {180 - len(EXCLUDED)} | {measured['all'].brier:.6f} |" in row
        assert "not the headline" in row
        assert "tuned on" in row
        held_out = next(line for line in text.splitlines() if line.startswith("| **test** |"))
        assert f"| {measured['test'].n} | {measured['test'].brier:.6f} |" in held_out
        assert "Held out" in held_out

    def test_dev_is_listed_as_the_tuning_partition(self, tmp_path: Path) -> None:
        text = _headline(tmp_path, _artifact())
        section = text[text.index("## Headline") :]
        row = next(line for line in section.splitlines() if line.startswith("| dev |"))
        assert "| 40 |" in row and "never quote it as a result" in row

    def test_metrics_json_marks_which_partition_is_the_headline(self, tmp_path: Path) -> None:
        directory = write_report(_artifact(), root=tmp_path)
        payload = json.loads((directory / "metrics.json").read_text())
        assert payload["partition"] == "test"
        flags = {row["partition"]: row["is_headline"] for row in payload["headline_by_partition"]}
        assert flags == {"test": True, "all": False, "dev": False}
        assert payload["configs"][0]["n"] == len(_declared().test)


class TestTheSplitIsStated:
    def test_what_when_and_against_which_manifest(self, tmp_path: Path) -> None:
        artifact = _artifact()
        text = _headline(tmp_path, artifact)
        section = text[text.index("## Dev/test split") : text.index("## Headline")]
        n_test = len(_declared().test)
        assert "**40 dev**" in section and f"**{n_test} test**" in section
        assert f"**{len(EXCLUDED)} of the 180 sealed scenarios" in section
        assert "placeholder" in section and "ADR-0043" in section
        assert "declared before any" in section and "forecast existed" in section
        assert artifact.split is not None and artifact.split.sha256 in section
        assert f"Applies to scenario manifest: `{'a' * 64}`" in section
        assert "no outcome, forecast or error" in section

    def test_tuning_is_only_legitimate_on_dev(self, tmp_path: Path) -> None:
        assert "**Tuning is only ever legitimate on dev.**" in _headline(tmp_path, _artifact())

    def test_the_split_section_precedes_the_headline(self, tmp_path: Path) -> None:
        text = _headline(tmp_path, _artifact())
        assert text.index("## Dev/test split") < text.index("## Headline")

    def test_the_domain_table_and_the_overlaps_are_printed(self, tmp_path: Path) -> None:
        text = _headline(tmp_path, _artifact())
        elections = next(row for row in _declared().domains if row.domain == "elections")
        assert f"| elections | {elections.n} | {elections.dev} | {elections.test} |" in text
        assert "| test | 140 | 74 | 75 / 65 |" in text
        assert "| dev | 40 | 16 | 18 / 22 |" in text
        assert "never crosses the dev/test boundary" in text

    def test_the_manifest_carries_the_split_and_the_dev_ids(self, tmp_path: Path) -> None:
        artifact = _artifact()
        payload = json.loads((write_report(artifact, root=tmp_path) / "manifest.json").read_text())
        assert payload["partition"] == "test"
        assert payload["split"]["sha256"] == load_settings().eval.split_sha256
        assert payload["split"]["declared_before_any_forecast"] is True
        assert payload["split"]["applies_to_manifest_sha256"] == "a" * 64
        assert (payload["split"]["n_dev"], payload["split"]["n_test"]) == (
            40,
            len(_declared().test),
        )
        assert {item["scenario_id"] for item in payload["split"]["excluded"]} == EXCLUDED
        assert payload["split"]["dev_scenario_ids"] == list(_declared().dev)

    def test_a_report_with_no_split_says_nothing_in_it_is_held_out(self, tmp_path: Path) -> None:
        text = _headline(tmp_path, _artifact(split=None, headline_partitions=(), partition="all"))
        assert "No dev/test split was supplied" in text
        assert "Nothing in it may be read as a held-out figure" in text

    def test_withheld_tuning_variants_are_named_not_scored(self, tmp_path: Path) -> None:
        text = _headline(tmp_path, _artifact(withheld_configs=("k9-try3",)))
        assert "k9-try3" in text and "not scored on this partition" in text


class TestOtherPartitionsAnnounceThemselves:
    def test_a_dev_report_says_it_is_not_a_result_before_any_number(self, tmp_path: Path) -> None:
        text = _headline(tmp_path, _artifact("dev"))
        assert "written on the DEV partition" in text
        assert text.index("written on the DEV partition") < text.index("**Brier")
        assert "none of them is a result" in text

    def test_an_all_scenario_report_points_at_the_held_out_row(self, tmp_path: Path) -> None:
        text = _headline(tmp_path, _artifact("all"))
        assert "written on ALL scenarios" in text
        assert text.index("written on ALL scenarios") < text.index("**Brier")

    def test_a_test_report_carries_no_such_banner(self, tmp_path: Path) -> None:
        text = _headline(tmp_path, _artifact())
        assert "written on the DEV" not in text and "written on ALL" not in text


def _comparison(name: str, a: str, family: str, p_adjusted: float) -> Comparison:
    return Comparison(
        name=name,
        config_a=a,
        config_b="C01",
        brier_a=0.21,
        brier_b=0.2,
        n_paired=74,
        interval=BootstrapInterval(point=0.01, lo=-0.01, hi=0.03, b=1000, p_value=0.2),
        p_adjusted=p_adjusted,
        family=family,  # type: ignore[arg-type]
    )


class TestFamiliesAreReportedApart:
    def test_supplementary_rows_are_under_their_own_heading(self, tmp_path: Path) -> None:
        artifact = _artifact(
            comparisons=(
                _comparison("LOO information asymmetry", "C05", "appendix_c", 0.4),
                _comparison("S01 vs C01 (supplementary)", "S01", "supplementary", 0.2),
            )
        )
        text = _headline(tmp_path, artifact)
        deltas = text[
            text.index("## Reading the deltas") : text.index("## Supplementary comparisons")
        ]
        extra = text[text.index("## Supplementary comparisons") :]
        assert "LOO information asymmetry" in deltas and "S01 vs C01" not in deltas
        assert "S01 vs C01" in extra
        assert "not part of Appendix C's twelve-cell family" in extra
        assert "family of their own" in extra
        assert "tuning" in extra and "on test" in extra
        assert "Holm-Bonferroni family of 1," in deltas and "| 74 |" in deltas

    def test_significance_json_states_each_family_s_size(self, tmp_path: Path) -> None:
        artifact = _artifact(
            comparisons=(
                _comparison("LOO information asymmetry", "C05", "appendix_c", 0.4),
                _comparison("LOO causal decomposition", "C09", "appendix_c", 0.4),
                _comparison("S01 vs C01 (supplementary)", "S01", "supplementary", 0.2),
            )
        )
        payload = json.loads(
            (write_report(artifact, root=tmp_path) / "significance.json").read_text()
        )
        assert payload["method"]["family_sizes"] == {
            "appendix_c": 2,
            "exploratory_dev": 0,
            "supplementary": 1,
        }
        assert payload["method"]["family_size"] == 2
        assert payload["method"]["partition"] == "test"
        assert [row["family"] for row in payload["comparisons"]] == [
            "appendix_c",
            "appendix_c",
            "supplementary",
        ]

    def test_with_no_supplementary_forecasts_the_section_says_how_to_get_them(
        self, tmp_path: Path
    ) -> None:
        assert "cascade eval grid --supplementary" in _headline(tmp_path, _artifact())

    def test_the_forest_figure_is_the_ablation_family_only(self, tmp_path: Path) -> None:
        artifact = _artifact(
            comparisons=(
                _comparison("LOO information asymmetry", "C05", "appendix_c", 0.4),
                _comparison("S01 vs C01 (supplementary)", "S01", "supplementary", 0.2),
            )
        )
        svg = (
            write_report(artifact, root=tmp_path) / "figures" / "ablation_forest.svg"
        ).read_text()
        assert "LOO information asymmetry" in svg and "S01" not in svg


class TestTheEvidenceTable:
    def _finding(self):
        rows = select(_scored(), _declared(), "test")
        counts = {item.scenario_id: (index * 911) % 25_000 for index, item in enumerate(rows)}
        counts[rows[0].scenario_id] = 0
        return evidence_finding(rows, counts, seed=5, permutations=200)

    def test_the_table_is_in_the_report_with_its_caveat(self, tmp_path: Path) -> None:
        text = _headline(tmp_path, _artifact(evidence=self._finding()))
        section = text[text.index("## Accuracy by evidence quality") :]
        assert "final 30 days before their cutoff" in section
        assert "not quantiles" in section
        assert "| none | 0 |" in section and "| rich | 10,000+ |" in section
        assert "| thin | 1-999 |" in section and "| moderate | 1,000-9,999 |" in section
        assert f"Measured on the test partition, {len(_declared().test)} scenarios" in section
        assert "does not estimate what more" in section

    def test_the_csv_has_the_per_domain_shape(self, tmp_path: Path) -> None:
        directory = write_report(_artifact(evidence=self._finding()), root=tmp_path)
        lines = (directory / "per_evidence_tier.csv").read_text().strip().splitlines()
        assert lines[0] == "tier,chunks_lo,chunks_hi,n,base_rate,brier"
        assert [line.split(",")[0] for line in lines[1:]] == ["none", "thin", "moderate", "rich"]
        assert sum(int(line.split(",")[3]) for line in lines[1:]) == len(_declared().test)

    def test_without_a_measurement_the_csv_is_a_header_and_the_section_is_absent(
        self, tmp_path: Path
    ) -> None:
        directory = write_report(_artifact(), root=tmp_path)
        assert (directory / "per_evidence_tier.csv").read_text().strip() == (
            "tier,chunks_lo,chunks_hi,n,base_rate,brier"
        )
        assert "## Accuracy by evidence quality" not in (directory / "headline.md").read_text()

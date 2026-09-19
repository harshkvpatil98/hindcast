"""The dev/test split at the CLI boundary, against a database that is not there.

The pure halves are tested where they live. This file is about the wiring --
the place a correct `split.py` can still be defeated: a command that forgets to
select a partition, a report that scores a tuning variant on test, a guard
whose refusal never becomes an exit status. The store is replaced wholesale and
`psycopg.connect` is booby-trapped, so nothing here can reach a real database.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import cascade.cli as cli_module
from cascade.cli import app
from cascade.config import load_settings, repo_root
from cascade.eval.ablation import cell_by_id, grid_scenarios
from cascade.eval.schema import ScoredForecast
from cascade.eval.split import HeldOutViolation, declare_split
from cascade.eval.store import FrozenSplit
from cascade.eval.supplementary import supplementary_by_id
from cascade.version import EXIT_OK, EXIT_PRECONDITION

runner = CliRunner()

REGISTRY: list[tuple[str, str]] = [
    (scenario_id, domain)
    for scenario_id, domain in json.loads(
        (repo_root() / "tests" / "fixtures" / "registry_domains.json").read_text(encoding="utf-8")
    )
]
IDS = [scenario_id for scenario_id, _ in REGISTRY]


def _declared():
    return declare_split(REGISTRY, salt=load_settings().study.salt, dev_size=40)


def _rows(
    config_id: str, ids: list[str], *, good_on_test: bool = True
) -> tuple[ScoredForecast, ...]:
    """Sharp and right on test, sharp and wrong on dev: Brier 0.01 against 0.81.

    So a figure that mixes the partitions cannot hide -- any dev scenario in
    the headline drags it visibly off 0.010000.
    """
    dev = set(_declared().dev)
    domain_of = dict(REGISTRY)
    out = []
    for scenario_id in ids:
        outcome = IDS.index(scenario_id) % 2
        right = (scenario_id not in dev) == good_on_test
        p_hat = (0.9 if outcome else 0.1) if right else (0.1 if outcome else 0.9)
        out.append(
            ScoredForecast(
                scenario_id=scenario_id,
                config_id=config_id,
                p_hat=p_hat,
                outcome=outcome,  # type: ignore[arg-type]
                domain=domain_of[scenario_id],
                n_replicates=30,
            )
        )
    return tuple(out)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A sealed registry, five stored configurations, and no database."""
    import psycopg

    settings = load_settings()
    salt = settings.study.salt
    subsample = list(
        grid_scenarios(
            IDS, cell=cell_by_id("C05"), salt=salt, cap=settings.ensemble.ablation_scenarios
        )
    )
    forecasts = {
        "C01": _rows("C01", IDS),
        "C05": _rows("C05", subsample, good_on_test=False),
        "S01": _rows("S01", subsample),
        "B2_single_direct": _rows("B2_single_direct", IDS, good_on_test=False),
        "k9-try3": _rows("k9-try3", list(_declared().dev)),
        "leaky-variant": _rows("leaky-variant", IDS),
    }
    state: dict[str, Any] = {"forecasts": forecasts, "reports": [], "registry": list(REGISTRY)}

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a unit test tried to open a database connection")

    monkeypatch.setattr(psycopg, "connect", refuse)
    monkeypatch.setattr(
        "cascade.eval.store.assert_frozen_split",
        lambda settings, **_: FrozenSplit(
            manifest_sha256="f" * 64,
            n_scenarios=180,
            n_yes=90,
            base_rate=0.5,
            climatology_brier=0.25,
            study_salt=salt,
        ),
    )
    monkeypatch.setattr(
        "cascade.ledger.store.load_scenarios",
        lambda settings, **_: tuple(
            SimpleNamespace(scenario_id=scenario_id, domain=domain)
            for scenario_id, domain in state["registry"]
        ),
    )
    monkeypatch.setattr(
        "cascade.eval.store.scored_forecasts",
        lambda settings, *, config_id, **_: state["forecasts"].get(config_id, ()),
    )
    monkeypatch.setattr(
        "cascade.eval.store.available_configs",
        lambda settings, **_: tuple(
            (name, len(rows)) for name, rows in sorted(state["forecasts"].items())
        ),
    )
    monkeypatch.setattr(
        "cascade.eval.store.forecast_scenarios",
        lambda settings, *, config_id, **_: tuple(
            item.scenario_id for item in state["forecasts"].get(config_id, ())
        ),
    )
    monkeypatch.setattr(
        "cascade.eval.store.evidence_counts",
        lambda settings, *, window_days, **_: {
            scenario_id: (index * 911) % 25_000 for index, scenario_id in enumerate(IDS)
        },
    )
    monkeypatch.setattr("cascade.eval.store.prompt_revision_audit", lambda settings, **_: [])
    monkeypatch.setattr(
        "cascade.eval.store.write_study_report",
        lambda settings, **fields: state["reports"].append(fields),
    )
    monkeypatch.setattr("cascade.ensemble.store.scores_by_scenario", lambda settings, **_: {})
    return state


def _report(tmp_path: Path, *extra: str) -> Path:
    result = runner.invoke(app, ["report", "--out", str(tmp_path), *extra])
    assert result.exit_code == EXIT_OK, result.output
    (directory,) = [path for path in tmp_path.iterdir() if path.name.startswith("study_")]
    return directory


class TestTheReportCommand:
    def test_the_headline_is_measured_on_test_alone(self, store: dict, tmp_path: Path) -> None:
        directory = _report(tmp_path)
        text = (directory / "headline.md").read_text()
        section = text[text.index("## Headline") :]
        lead = section[section.index("**Brier") :].splitlines()[0]
        assert "**Brier 0.010000**" in lead and "140 scenarios" in lead
        payload = json.loads((directory / "metrics.json").read_text())
        headline = next(row for row in payload["configs"] if row["config_id"] == "C01")
        assert (headline["n"], headline["brier"]) == (140, pytest.approx(0.01))

    def test_the_all_scenario_figure_is_printed_beside_it_labelled(
        self, store: dict, tmp_path: Path
    ) -> None:
        text = (_report(tmp_path) / "headline.md").read_text()
        section = text[text.index("## Headline") :]
        row = next(line for line in section.splitlines() if line.startswith("| all |"))
        expected = (140 * 0.01 + 40 * 0.81) / 180
        assert f"| 180 | {expected:.6f} |" in row and "not the headline" in row

    def test_the_database_row_records_the_test_figure_and_says_so(
        self, store: dict, tmp_path: Path
    ) -> None:
        _report(tmp_path)
        (row,) = store["reports"]
        assert row["headline_brier"] == pytest.approx(0.01)
        assert "test partition" in row["notes"]

    def test_no_dev_scenario_reaches_any_per_scenario_file(
        self, store: dict, tmp_path: Path
    ) -> None:
        directory = _report(tmp_path)
        dev = set(_declared().dev)
        for name in ("ablation_grid.csv", "baselines.csv"):
            rows = (directory / name).read_text().strip().splitlines()[1:]
            assert rows, name
            assert not {line.split(",")[1] for line in rows} & dev, name

    def test_a_tuning_variant_is_not_scored_on_test(self, store: dict, tmp_path: Path) -> None:
        """Its held-out forecasts exist in the store -- `leaky-variant` has all
        180 -- and the report must not be the place they get looked at."""
        directory = _report(tmp_path)
        payload = json.loads((directory / "metrics.json").read_text())
        scored = {row["config_id"] for row in payload["configs"]}
        assert scored == {"C01", "C05", "S01", "B2_single_direct"}
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["split"]["configs_withheld_from_this_partition"] == [
            "k9-try3",
            "leaky-variant",
        ]

    def test_a_dev_report_scores_the_variants_and_says_what_it_is(
        self, store: dict, tmp_path: Path
    ) -> None:
        directory = _report(tmp_path, "--partition", "dev")
        payload = json.loads((directory / "metrics.json").read_text())
        assert {"k9-try3", "leaky-variant"} <= {row["config_id"] for row in payload["configs"]}
        assert all(row["n"] <= 40 for row in payload["configs"])
        assert "written on the DEV partition" in (directory / "headline.md").read_text()

    def test_s01_is_reported_in_its_own_family_on_test_scenarios(
        self, store: dict, tmp_path: Path
    ) -> None:
        payload = json.loads((_report(tmp_path) / "significance.json").read_text())
        families = {row["name"]: (row["family"], row["n_paired"]) for row in payload["comparisons"]}
        assert families["S01 vs C01 (supplementary)"] == ("supplementary", 74)
        assert families["LOO information asymmetry"] == ("appendix_c", 74)
        assert not any("variant" in name or "k9" in name for name in families)

    def test_the_evidence_table_is_written_for_the_test_partition(
        self, store: dict, tmp_path: Path
    ) -> None:
        lines = (_report(tmp_path) / "per_evidence_tier.csv").read_text().strip().splitlines()
        assert sum(int(line.split(",")[3]) for line in lines[1:]) == 140

    def test_an_unknown_partition_is_refused(self, store: dict, tmp_path: Path) -> None:
        result = runner.invoke(app, ["report", "--out", str(tmp_path), "--partition", "holdout"])
        assert result.exit_code == EXIT_PRECONDITION


class TestEvalScore:
    def test_it_measures_on_dev_unless_told_otherwise(self, store: dict) -> None:
        result = runner.invoke(app, ["eval", "score", "--config-id", "C01"])
        assert result.exit_code == EXIT_OK, result.output
        assert "measuring on dev" in result.output
        assert "0.810000" in result.output and "0.010000" not in result.output

    def test_test_is_available_to_a_declared_configuration_on_request(self, store: dict) -> None:
        result = runner.invoke(app, ["eval", "score", "--config-id", "C01", "--partition", "test"])
        assert result.exit_code == EXIT_OK, result.output
        assert "0.010000" in result.output and "held-out" in result.output

    @pytest.mark.parametrize("partition", ["test", "all"])
    def test_a_tuning_variant_is_refused_off_dev(self, store: dict, partition: str) -> None:
        result = runner.invoke(
            app, ["eval", "score", "--config-id", "leaky-variant", "--partition", partition]
        )
        assert result.exit_code == EXIT_PRECONDITION
        assert "0.010000" not in result.output

    def test_a_tuning_variant_is_scoreable_on_dev(self, store: dict) -> None:
        result = runner.invoke(app, ["eval", "score", "--config-id", "leaky-variant"])
        assert result.exit_code == EXIT_OK, result.output
        assert "0.810000" in result.output


class TestEvalSignificance:
    def test_a_variant_comparison_is_refused_off_dev(self, store: dict) -> None:
        result = runner.invoke(
            app, ["eval", "significance", "--partition", "test", "--versus", "k9-try3"]
        )
        assert result.exit_code == EXIT_PRECONDITION

    def test_on_dev_it_is_its_own_labelled_family(self, store: dict) -> None:
        # Widened: rich folds a narrow cell, and a substring would see the wrap.
        result = runner.invoke(
            app, ["eval", "significance", "--versus", "k9-try3"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == EXIT_OK, result.output
        assert "exploratory_dev" in result.output and "supplementary" in result.output

    def test_a_declared_cell_is_not_a_variant(self, store: dict) -> None:
        result = runner.invoke(app, ["eval", "significance", "--versus", "C05"])
        assert result.exit_code == EXIT_PRECONDITION


class TestTuneGuard:
    def test_a_dev_only_variant_passes(self, store: dict) -> None:
        result = runner.invoke(app, ["eval", "tune-guard", "--config-id", "k9-try3"])
        assert result.exit_code == EXIT_OK, result.output
        assert "dev only" in result.output

    def test_a_variant_that_touched_test_is_refused(self, store: dict) -> None:
        result = runner.invoke(app, ["eval", "tune-guard", "--config-id", "leaky-variant"])
        assert result.exit_code == EXIT_PRECONDITION

    def test_one_held_out_scenario_in_a_list_is_refused(self, store: dict) -> None:
        declared = _declared()
        arguments = ["eval", "tune-guard"]
        for scenario_id in (*declared.dev[:3], declared.test[0]):
            arguments += ["--scenario", scenario_id]
        assert runner.invoke(app, arguments).exit_code == EXIT_PRECONDITION
        assert runner.invoke(app, arguments[:-2]).exit_code == EXIT_OK

    def test_nothing_to_check_is_not_a_pass(self, store: dict) -> None:
        assert runner.invoke(app, ["eval", "tune-guard"]).exit_code == EXIT_PRECONDITION

    def test_a_config_with_no_forecasts_is_not_vouched_for(self, store: dict) -> None:
        result = runner.invoke(app, ["eval", "tune-guard", "--config-id", "never-run"])
        assert result.exit_code == EXIT_PRECONDITION


class TestTheDeclarationIsChecked:
    def test_eval_split_prints_the_declared_split(self, store: dict) -> None:
        result = runner.invoke(app, ["eval", "split"])
        assert result.exit_code == EXIT_OK, result.output
        assert "40 dev / 140 test" in result.output
        assert str(load_settings().eval.split_sha256)[:16] in result.output.replace("\n", "")

    def test_it_lists_a_partition_s_ids_on_request(self, store: dict) -> None:
        result = runner.invoke(app, ["eval", "split", "--ids", "dev"])
        assert result.exit_code == EXIT_OK, result.output
        assert (
            sum(line.strip() in set(_declared().dev) for line in result.output.splitlines()) == 40
        )

    def test_a_changed_dev_size_is_refused_everywhere(
        self, store: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The environment can override `eval.dev_scenarios`. It cannot do so
        quietly: the recomputed split no longer matches the pin."""
        from cascade.config import _cached_settings

        monkeypatch.setenv("CASCADE_EVAL__DEV_SCENARIOS", "41")
        _cached_settings.cache_clear()
        for arguments in (
            ["eval", "split"],
            ["eval", "score", "--config-id", "C01"],
            ["eval", "significance"],
            ["report", "--out", str(tmp_path)],
        ):
            assert runner.invoke(app, arguments).exit_code == EXIT_PRECONDITION, arguments
        assert list(tmp_path.iterdir()) == []

    def test_a_registry_that_is_not_the_sealed_one_is_refused(self, store: dict) -> None:
        """Refused *as an incomplete registry*, before the pin is consulted.

        The pin would refuse this too -- 179 scenarios cannot reproduce a
        fingerprint over 180 -- but with the wrong diagnosis: "the split moved,
        do not re-pin" sends an operator looking for an edited salt when the
        fact is a registry that does not match its seal. The message is
        asserted because the exit code alone cannot tell the two apart.
        """
        store["registry"] = REGISTRY[:-1]
        result = runner.invoke(app, ["eval", "score", "--config-id", "C01"], env={"COLUMNS": "400"})
        assert result.exit_code == EXIT_PRECONDITION
        assert "holds 179 scenarios but the seal covers 180" in result.output
        assert "re-pin" not in result.output

    def test_a_moved_registry_of_the_right_size_is_refused_by_the_pin(self, store: dict) -> None:
        store["registry"] = [*REGISTRY[:-1], ("polymarket:a-substitute", "elections")]
        result = runner.invoke(app, ["eval", "score", "--config-id", "C01"], env={"COLUMNS": "400"})
        assert result.exit_code == EXIT_PRECONDITION
        assert "Do not re-pin" in result.output


class TestTheErrorBoundary:
    def test_a_guard_refusal_from_anywhere_exits_three(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tuning path that never thought to catch the guard still cannot
        turn its refusal into a traceback and an exit 1."""

        def boom(*args: object, **kwargs: object) -> None:
            raise HeldOutViolation("a sweep refused: 1 of 41 scenario(s) are held out")

        monkeypatch.setattr(cli_module, "app", boom)
        monkeypatch.setattr(sys, "argv", ["cascade", "eval", "score"])
        assert cli_module.main() == EXIT_PRECONDITION


class TestGridCellSelection:
    def test_the_default_grid_is_the_twelve_and_only_the_twelve(self) -> None:
        cells = cli_module._grid_cells(None, supplementary=False, variants=[])
        assert [cell.cell_id for cell in cells] == [f"C{index:02d}" for index in range(1, 13)]

    def test_supplementary_cells_join_only_when_asked_for(self) -> None:
        cells = cli_module._grid_cells(None, supplementary=True, variants=[])
        assert [cell.cell_id for cell in cells][-1] == "S01" and len(cells) == 13
        assert cli_module._grid_cells(["S01"], supplementary=True, variants=[]) == [
            supplementary_by_id("S01")
        ]

    def test_naming_s01_without_the_flag_is_refused(self) -> None:

        with pytest.raises(typer.Exit) as caught:
            cli_module._grid_cells(["S01"], supplementary=False, variants=[])
        assert caught.value.exit_code == EXIT_PRECONDITION

    def test_a_variant_must_be_a_tuning_overlay(self) -> None:

        for name in ("C05", "S01", "no-such-overlay"):
            with pytest.raises(typer.Exit):
                cli_module._grid_cells(None, supplementary=False, variants=[name])

    def test_the_flag_is_on_the_command(self) -> None:
        output = runner.invoke(app, ["eval", "grid", "--help"]).output
        assert "--supplementary" in output and "--variant" in output and "--partition" in output


class TestGridScenarioSelection:
    def _select(self, cell: Any, **overrides: Any) -> list[str]:
        settings = load_settings()
        arguments: dict[str, Any] = {
            "salt": settings.study.salt,
            "cap": settings.ensemble.ablation_scenarios,
            "declaration": _declared(),
            "partition": "all",
            "is_variant": False,
            "limit": None,
        }
        arguments.update(overrides)
        return cli_module._cell_scenarios(cell, IDS, **arguments)

    def test_a_study_run_is_unchanged(self) -> None:
        settings = load_settings()
        assert self._select(cell_by_id("C01")) == sorted(IDS)
        assert self._select(cell_by_id("C05")) == list(
            grid_scenarios(IDS, cell=cell_by_id("C05"), salt=settings.study.salt, cap=90)
        )

    def test_s01_runs_the_same_subsample_as_every_capped_cell(self) -> None:
        assert self._select(supplementary_by_id("S01")) == self._select(cell_by_id("C05"))

    def test_a_partition_is_an_intersection_of_the_cell_s_own_scenarios(self) -> None:
        dev = self._select(cell_by_id("C05"), partition="dev")
        assert len(dev) == 16 and set(dev) <= set(_declared().dev)
        assert set(dev) == set(self._select(cell_by_id("C05"))) & set(_declared().dev)

    def test_a_variant_gets_all_of_dev_and_nothing_else(self) -> None:
        from cascade.eval.ablation import CellSpec

        variant = CellSpec("k9-try3", True, True, "chronofence", 200, "variant")
        chosen = self._select(variant, partition="dev", is_variant=True)
        assert chosen == list(_declared().dev)

    def test_a_variant_asked_to_run_anywhere_else_is_refused(self) -> None:

        from cascade.eval.ablation import CellSpec

        variant = CellSpec("k9-try3", True, True, "chronofence", 200, "variant")
        for partition in ("all", "test"):
            with pytest.raises(typer.Exit) as caught:
                self._select(variant, partition=partition, is_variant=True)
            assert caught.value.exit_code == EXIT_PRECONDITION

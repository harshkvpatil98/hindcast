"""Stand-ins declared unscoreable before any forecast (ADR-0043).

The owner kept the sealed registry exactly as sealed and chose to exclude the
exchange placeholder legs from scoring instead. What has to hold: the rule
reads wording only; it catches exactly the stand-ins in the sealed set and no
real name; excluded scenarios are on neither side of the split and in no
scored figure; and the exclusion is inside the pinned fingerprint, so it
cannot be widened after forecasts exist without the pin refusing.
"""

from __future__ import annotations

import inspect
import json

import pytest

from cascade.config import load_settings, repo_root
from cascade.eval.schema import ScoredForecast
from cascade.eval.split import (
    ExcludedFromScoring,
    HeldOutViolation,
    SplitDeclarationMismatch,
    _fingerprint,
    assert_declared,
    declare_split,
    declare_study_split,
    require_dev_only,
    select,
)
from cascade.ledger.exclusions import exclusions, is_placeholder

STUDY: list[tuple[str, str, str]] = [
    (scenario_id, domain, question)
    for scenario_id, domain, question in json.loads(
        (repo_root() / "tests" / "fixtures" / "registry_domains.json").read_text(encoding="utf-8")
    )
]


def _study():
    return declare_study_split(STUDY, salt=load_settings().study.salt, dev_size=40)


@pytest.mark.parametrize(
    "question",
    [
        "Will Placeholder V be the #1 searched song on Google this year?",
        "Will Candidate B win the 2026 Busan Mayoral Election?",
        "Will Company K be the largest company in the world by market cap on June 30?",
        "Will Person C be the 2025 Drivers Champion?",
        "Will Player BI win the 2026 TOUR Championship?",
        "Will player L record the most assists in the 2025-26 UEFA Champions League?",
        "Will Team B make the first pick of the 2025 NFL Draft?",
        "Will Country N be the Jury Winner in the Eurovision 2026 Grand Final?",
    ],
)
def test_a_stand_in_is_caught(question: str) -> None:
    assert is_placeholder(question)


@pytest.mark.parametrize(
    "question",
    [
        "Will NVIDIA be the largest company in the world by market cap on July 31?",
        "Will Team USA win the most gold medals at the Games?",
        "Will company 3M be acquired?",
        "Will another player win the HLTV Player of the Year award?",
        "Will the candidate who wins form a government?",
        "Will a Democrat win Maine US Senate Election?",
    ],
)
def test_a_real_name_or_plain_prose_is_not(question: str) -> None:
    assert not is_placeholder(question)


def test_the_rule_reads_wording_and_nothing_else() -> None:
    assert list(inspect.signature(is_placeholder).parameters) == ["question"]
    assert list(inspect.signature(exclusions).parameters) == ["scenarios"]
    assert list(inspect.signature(declare_study_split).parameters) == [
        "scenarios",
        "salt",
        "dev_size",
    ]


def test_exactly_fifteen_of_the_sealed_scenarios_are_excluded() -> None:
    """A regression pin on the rule over the real registry: widening or
    narrowing it moves this number, and the report quotes it."""
    excluded = exclusions([(scenario_id, question) for scenario_id, _, question in STUDY])
    assert len(excluded) == 15
    assert {item.reason for item in excluded} == {"placeholder_leg"}
    assert exclusions(list(reversed([(i, q) for i, _, q in STUDY]))) == excluded


def test_excluded_scenarios_are_on_neither_side_and_the_sizes_hold() -> None:
    declared = _study()
    excluded = set(declared.excluded_ids)
    assert not excluded & set(declared.dev) and not excluded & set(declared.test)
    assert len(declared.dev) == 40
    assert len(declared.dev) + len(declared.test) + len(excluded) == len(STUDY)


def test_no_scored_figure_can_contain_an_excluded_scenario() -> None:
    declared = _study()
    rows = [
        ScoredForecast(
            scenario_id=scenario_id, config_id="C01", p_hat=0.5, outcome=0, domain=domain
        )
        for scenario_id, domain, _ in STUDY
    ]
    for partition in ("all", "dev", "test"):
        chosen = {item.scenario_id for item in select(rows, declared, partition)}  # type: ignore[arg-type]
        assert not chosen & set(declared.excluded_ids)
    assert len(select(rows, declared, "all")) == len(STUDY) - len(declared.excluded)


def test_an_excluded_scenario_cannot_be_tuned_on() -> None:
    declared = _study()
    with pytest.raises(ExcludedFromScoring):
        declared.partition_of(declared.excluded_ids[0])
    with pytest.raises(HeldOutViolation, match="1 are excluded from scoring"):
        require_dev_only([declared.excluded_ids[0]], declared, what="a tuning run")


def test_the_exclusion_is_inside_the_pin() -> None:
    """Widening the rule afterwards -- dropping one more scenario that went
    badly -- must change the fingerprint and be refused."""
    declared = _study()
    assert_declared(declared, pinned_sha256=load_settings().eval.split_sha256)
    assert declared.sha256 == _fingerprint(declared.dev, declared.test, declared.excluded)
    assert declared.sha256 != _fingerprint(declared.dev, declared.test)
    widened = declare_study_split(
        [(i, d, "Will Candidate Z win?" if i == declared.test[0] else q) for i, d, q in STUDY],
        salt=load_settings().study.salt,
        dev_size=40,
    )
    with pytest.raises(SplitDeclarationMismatch):
        assert_declared(widened, pinned_sha256=load_settings().eval.split_sha256)


def test_with_nothing_to_exclude_the_study_split_is_the_plain_split() -> None:
    """The exclusion adds to the fingerprint only when there is something to
    exclude, so the plain split's semantics are unchanged."""
    plain = [(f"s{index:03d}", "elections", f"Will Alpha{index} win?") for index in range(60)]
    study = declare_study_split(plain, salt="s", dev_size=10)
    assert study == declare_split([(i, d) for i, d, _ in plain], salt="s", dev_size=10)


def test_the_spend_paths_skip_what_scoring_would_discard() -> None:
    """Compiling, briefing or forecasting "Candidate B" buys a number the report
    must throw away. Every command that spends on a scenario filters first."""
    import ast
    from types import SimpleNamespace

    from cascade import cli

    kept = cli._scorable(
        [
            SimpleNamespace(scenario_id="a", question="Will Candidate B win the election?"),
            SimpleNamespace(scenario_id="b", question="Will Alpha win the election?"),
        ],
        what="a test",
    )
    assert [item.scenario_id for item in kept] == ["b"]

    tree = ast.parse((repo_root() / "cascade" / "cli.py").read_text(encoding="utf-8"))
    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_scorable"
            for call in ast.walk(node)
        )
    }
    assert {
        "compile_build",
        "compile_dossier",
        "eval_baselines",
        "eval_estimate",
        "eval_injection",
    } <= callers

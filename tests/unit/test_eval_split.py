"""The declared dev/test split (`cascade/eval/split.py`).

The split is what makes tuning legitimate, so the tests are about the ways it
could quietly stop doing that: membership that depends on an outcome, on the
order scenarios were loaded in, or on which scenarios happen to be scored; a
dev set that overlaps the test set; a guard that lets a held-out scenario by.

`tests/fixtures/registry_domains.json` is the sealed registry's 180 scenario
ids and their domains -- and nothing else. No label is in it, so nothing here
could depend on one even by accident.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections import Counter
from types import SimpleNamespace

import pytest

from cascade.config import load_settings, repo_root
from cascade.eval.ablation import cell_by_id, grid_scenarios
from cascade.eval.score import recalibration_half
from cascade.eval.split import (
    SPLIT_PURPOSE,
    HeldOutViolation,
    SplitDeclarationMismatch,
    SplitError,
    UnknownScenario,
    assert_declared,
    declare_split,
    domain_quotas,
    interactions,
    require_declared_config,
    require_dev_only,
    select,
    split_score,
)

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
DEV_SIZE = 40


def _salt() -> str:
    return load_settings().study.salt


def _declared():
    return declare_split(REGISTRY, salt=_salt(), dev_size=DEV_SIZE)


def _study_declared():
    """What every eval path declares: stand-ins excluded, then split."""
    from cascade.eval.split import declare_study_split

    return declare_study_split(STUDY, salt=_salt(), dev_size=DEV_SIZE)


class TestOutcomeIndependenceByConstruction:
    def test_the_signature_has_no_parameter_a_label_could_travel_through(self) -> None:
        """Ids, domains, a salt and a size. If a fifth parameter ever appears
        here, the reviewer's first question is what it lets the split see."""
        parameters = inspect.signature(declare_split).parameters
        assert list(parameters) == ["scenarios", "salt", "dev_size"]
        assert parameters["scenarios"].annotation == "Sequence[tuple[str, str]]"

    def test_a_labelled_record_is_not_an_acceptable_input(self) -> None:
        """The input is a pair. A scored forecast -- the labelled type -- is not
        one, and is refused rather than unpacked for its id."""
        from cascade.eval.schema import ScoredForecast

        labelled = ScoredForecast(
            scenario_id="s", config_id="C01", p_hat=0.5, outcome=1, domain="elections"
        )
        with pytest.raises((TypeError, ValueError)):
            declare_split([labelled], salt="s", dev_size=1)  # type: ignore[list-item]

    def test_the_declaration_has_no_field_an_outcome_could_occupy(self) -> None:
        fields = set(type(_declared()).model_fields)
        assert fields == {"purpose", "dev_size", "dev", "test", "domains", "sha256", "excluded"}
        # And the excluded entries carry an id and a reason, nothing else.
        from cascade.eval.exclusions import ExcludedScenario

        assert set(ExcludedScenario.model_fields) == {"scenario_id", "reason"}

    def test_the_module_cannot_name_a_label(self) -> None:
        """`cascade.eval.schema` holds `ScoredForecast`, the one labelled type;
        `split.py` must not import it, nor the store that joins labels, nor
        touch any attribute a label or a forecast would be read from."""
        import ast

        source = (repo_root() / "cascade" / "eval" / "split.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert imported == {
            "__future__",
            "hashlib",
            "collections.abc",
            "typing",
            "pydantic",
            "cascade.canonical",
            "cascade.eval.exclusions",
        }
        touched = (
            {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            | {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)}
        )
        assert touched.isdisjoint({"outcome", "outcomes", "label", "labels", "p_hat", "abs_error"})


class TestDeterminism:
    def test_the_same_inputs_give_the_same_split(self) -> None:
        assert _declared() == _declared()

    def test_input_order_does_not_matter(self) -> None:
        assert declare_split(list(reversed(REGISTRY)), salt=_salt(), dev_size=DEV_SIZE) == (
            _declared()
        )

    def test_a_different_salt_gives_a_different_split(self) -> None:
        other = declare_split(REGISTRY, salt="another-study", dev_size=DEV_SIZE)
        assert other.dev != _declared().dev
        assert other.sha256 != _declared().sha256

    def test_the_score_depends_on_the_purpose_string(self) -> None:
        """Domain separation: the same id under the same salt must not hash to
        what the ablation subsample and the recalibration halves hash it to."""
        scenario_id = REGISTRY[0][0]
        bare = hashlib.blake2b(scenario_id.encode(), digest_size=8, key=_salt().encode()).digest()
        assert split_score(scenario_id, salt=_salt()) != bare
        assert SPLIT_PURPOSE.endswith("/v1")


class TestThePartition:
    def test_it_is_the_declared_size(self) -> None:
        declared = _declared()
        assert len(declared.dev) == DEV_SIZE
        assert len(declared.test) == len(REGISTRY) - DEV_SIZE
        assert load_settings().eval.dev_scenarios == DEV_SIZE

    def test_dev_and_test_are_disjoint_and_exhaustive(self) -> None:
        declared = _declared()
        assert set(declared.dev) & set(declared.test) == set()
        assert set(declared.dev) | set(declared.test) == {
            scenario_id for scenario_id, _ in REGISTRY
        }
        assert declared.ids("all") == tuple(sorted(scenario_id for scenario_id, _ in REGISTRY))

    def test_every_scenario_has_exactly_one_partition(self) -> None:
        declared = _declared()
        sides = Counter(declared.partition_of(scenario_id) for scenario_id, _ in REGISTRY)
        assert sides == {"dev": DEV_SIZE, "test": len(REGISTRY) - DEV_SIZE}

    def test_a_duplicate_id_is_refused(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            declare_split([("a", "x"), ("a", "x"), ("b", "x")], salt="s", dev_size=1)

    def test_an_undeclared_id_has_no_partition(self) -> None:
        with pytest.raises(UnknownScenario):
            _declared().partition_of("polymarket:never-sealed")


class TestDomainBalance:
    """Stated tolerance: every domain's dev count is within one scenario of its
    proportional share, ``dev_size * n_d / n`` (the quota rule)."""

    @staticmethod
    def _counted() -> dict[str, tuple[int, int]]:
        """``{domain: (n, dev)}`` counted from the fixture's own domains.

        Deliberately not read from ``declared.domains``: that is the split's
        account of itself, and a split that stopped stratifying would report
        one well-balanced domain called "everything".
        """
        dev = set(_declared().dev)
        sizes = Counter(domain for _, domain in REGISTRY)
        in_dev = Counter(domain for scenario_id, domain in REGISTRY if scenario_id in dev)
        return {domain: (sizes[domain], in_dev[domain]) for domain in sorted(sizes)}

    def test_every_domain_is_within_one_of_its_proportional_share(self) -> None:
        counted = self._counted()
        assert len(counted) == 11
        for domain, (n, dev) in sorted(counted.items()):
            share = DEV_SIZE * n / len(REGISTRY)
            assert abs(dev - share) < 1, (domain, dev, share)

    def test_every_domain_large_enough_to_earn_a_scenario_has_one(self) -> None:
        """A dev set with no elections questions is a poor dev set."""
        for domain, (n, dev) in sorted(self._counted().items()):
            if DEV_SIZE * n / len(REGISTRY) >= 1:
                assert dev >= 1, domain

    def test_no_domain_is_tuned_on_in_its_entirety(self) -> None:
        assert all(dev < n for n, dev in self._counted().values())

    def test_the_split_s_own_account_of_its_domains_is_true(self) -> None:
        assert {row.domain: (row.n, row.dev) for row in _declared().domains} == self._counted()
        assert all(row.dev + row.test == row.n for row in _declared().domains)

    def test_the_allocation_matches_the_members(self) -> None:
        declared = _declared()
        domain_of = dict(REGISTRY)
        assert Counter(domain_of[scenario_id] for scenario_id in declared.dev) == {
            row.domain: row.dev for row in declared.domains if row.dev
        }

    def test_the_sealed_registry_s_allocation_is_the_declared_one(self) -> None:
        """The composition, written down. A change here is a changed split."""
        assert {row.domain: (row.dev, row.test) for row in _declared().domains} == {
            "conflict": (3, 8),
            "corporate": (0, 2),
            "elections": (10, 35),
            "geopolitics": (1, 5),
            "health": (2, 9),
            "labor": (1, 3),
            "macro_policy": (2, 9),
            "other": (8, 28),
            "regulation": (1, 2),
            "sports": (7, 22),
            "technology": (5, 17),
        }


class TestQuotas:
    def test_they_sum_to_the_dev_size_exactly(self) -> None:
        sizes = Counter(domain for _, domain in REGISTRY)
        for dev_size in (1, 17, 40, 90, len(REGISTRY) - len(sizes)):
            assert sum(domain_quotas(sizes, dev_size=dev_size).values()) == dev_size

    def test_an_exact_tie_goes_to_the_larger_domain(self) -> None:
        """29*40/180, 11*40/180 and 2*40/180 share a fractional part *exactly*;
        floats would break the tie by rounding noise. One seat is left after
        the clear winner, and the largest of the three tied domains takes it."""
        quotas = domain_quotas(
            {"sports": 29, "conflict": 11, "corporate": 2, "rest": 138}, dev_size=40
        )
        assert quotas == {"sports": 7, "conflict": 2, "corporate": 0, "rest": 31}

    def test_a_tie_between_equal_domains_goes_by_name(self) -> None:
        assert domain_quotas({"a": 2, "b": 2, "c": 2, "d": 3}, dev_size=3) == {
            "a": 1,
            "b": 1,
            "c": 0,
            "d": 1,
        }

    def test_a_dev_size_that_would_empty_a_domain_s_test_side_is_refused(self) -> None:
        with pytest.raises(ValueError, match="test keeps one of each"):
            domain_quotas({"a": 2, "b": 2}, dev_size=3)

    def test_a_single_scenario_domain_stays_in_test(self) -> None:
        assert domain_quotas({"solo": 1, "rest": 9}, dev_size=8)["solo"] == 0


class TestThePin:
    def test_the_pinned_fingerprint_is_the_sealed_registry_s_split(self) -> None:
        """The declaration, as a checkable fact. `eval.split_sha256` was
        committed before any forecast existed; this asserts that it is the
        fingerprint of the split this code produces over the sealed ids, so the
        pin, the code and the registry cannot drift apart unnoticed."""
        assert load_settings().eval.split_sha256 == _study_declared().sha256

    def test_the_declared_split_passes(self) -> None:
        assert_declared(_study_declared(), pinned_sha256=load_settings().eval.split_sha256)

    @pytest.mark.parametrize(
        "change",
        [
            {"salt": "cascade-2026-study-02"},
            {"dev_size": 41},
        ],
    )
    def test_a_changed_salt_or_size_is_refused(self, change: dict[str, object]) -> None:
        arguments: dict[str, object] = {"salt": _salt(), "dev_size": DEV_SIZE, **change}
        moved = declare_split(REGISTRY, **arguments)  # type: ignore[arg-type]
        with pytest.raises(SplitDeclarationMismatch, match="Do not re-pin"):
            assert_declared(moved, pinned_sha256=load_settings().eval.split_sha256)

    def test_a_changed_registry_is_refused(self) -> None:
        moved = declare_split(
            [*REGISTRY, ("polymarket:a-newcomer", "elections")], salt=_salt(), dev_size=DEV_SIZE
        )
        with pytest.raises(SplitDeclarationMismatch):
            assert_declared(moved, pinned_sha256=load_settings().eval.split_sha256)

    def test_an_unpinned_split_is_refused_not_waved_through(self) -> None:
        with pytest.raises(SplitDeclarationMismatch, match="no dev/test split is pinned"):
            assert_declared(_declared(), pinned_sha256=None)

    def test_the_fingerprint_binds_membership(self) -> None:
        """Swap one scenario across the line and the fingerprint changes: the
        pin identifies *which* scenarios are held out, not merely how many."""
        from cascade.eval.split import _fingerprint

        declared = _declared()
        assert _fingerprint(declared.dev, declared.test) == declared.sha256
        swapped_dev = tuple(sorted((*declared.dev[1:], declared.test[0])))
        swapped_test = tuple(sorted((declared.dev[0], *declared.test[1:])))
        assert _fingerprint(swapped_dev, swapped_test) != declared.sha256


class TestStabilityOnTheSealedRegistry:
    """Exact counts cost some stability. These bound how much."""

    @pytest.mark.parametrize("domain", sorted({domain for _, domain in REGISTRY}))
    def test_adding_a_scenario_moves_only_boundary_scenarios(self, domain: str) -> None:
        before = _declared()
        was_dev = set(before.dev)
        quota_before = {row.domain: row.dev for row in before.domains}
        domain_of = dict(REGISTRY)
        for index in range(12):
            newcomer = (f"hypothetical:{domain}:{index}", domain)
            after = declare_split([*REGISTRY, newcomer], salt=_salt(), dev_size=DEV_SIZE)
            quota_after = {row.domain: row.dev for row in after.domains}
            moved = Counter(
                domain_of[scenario_id]
                for scenario_id, _ in REGISTRY
                if (scenario_id in was_dev) != (scenario_id in set(after.dev))
            )
            for name in sorted(quota_after):
                allowed = abs(quota_after[name] - quota_before[name]) + (name == domain)
                assert moved[name] <= allowed, (newcomer, name, moved[name], allowed)
            assert sum(moved.values()) <= 4, (newcomer, moved)

    def test_dev_is_a_prefix_of_one_fixed_order_in_every_domain(self) -> None:
        """What a per-id score buys: the order is a property of the ids, so a
        changed registry can only move where the line falls, never who is on
        which side of whom."""
        declared = _declared()
        dev = set(declared.dev)
        by_domain: dict[str, list[str]] = {}
        for scenario_id, domain in REGISTRY:
            by_domain.setdefault(domain, []).append(scenario_id)
        for members in by_domain.values():
            ranked = sorted(members, key=lambda scenario_id: split_score(scenario_id, salt=_salt()))
            flags = [scenario_id in dev for scenario_id in ranked]
            assert flags == sorted(flags, reverse=True)

    def test_removing_a_scenario_moves_at_most_two(self) -> None:
        was_dev = set(_declared().dev)
        for index in range(0, len(REGISTRY), 7):
            rest = REGISTRY[:index] + REGISTRY[index + 1 :]
            after = set(declare_split(rest, salt=_salt(), dev_size=DEV_SIZE).dev)
            moved = [sid for sid, _ in rest if (sid in was_dev) != (sid in after)]
            assert len(moved) <= 2, (REGISTRY[index], moved)


class TestSelect:
    ROWS = staticmethod(
        lambda ids: [SimpleNamespace(scenario_id=scenario_id) for scenario_id in ids]
    )

    def test_a_subset_is_intersected_with_the_declaration_never_resplit(self) -> None:
        """The contamination channel a rank-based split opens if it is ever
        recomputed over "whatever is scored": the partition of a subset must
        be the declared partition, restricted."""
        declared = _declared()
        subset = [scenario_id for scenario_id, _ in REGISTRY][::3]
        chosen = select(self.ROWS(subset), declared, "dev")
        assert {row.scenario_id for row in chosen} == set(subset) & set(declared.dev)
        resplit = declare_split(
            [pair for pair in REGISTRY if pair[0] in set(subset)], salt=_salt(), dev_size=13
        )
        assert set(resplit.dev) != set(subset) & set(
            declared.dev
        ), "the fixture no longer demonstrates the hazard"

    def test_dev_and_test_partition_the_input(self) -> None:
        declared = _declared()
        rows = self.ROWS(scenario_id for scenario_id, _ in REGISTRY)
        dev = select(rows, declared, "dev")
        test = select(rows, declared, "test")
        assert len(dev) == DEV_SIZE and len(test) == len(REGISTRY) - DEV_SIZE
        assert {row.scenario_id for row in dev}.isdisjoint(row.scenario_id for row in test)
        assert len(select(rows, declared, "all")) == len(REGISTRY)

    def test_the_result_is_sorted_by_scenario(self) -> None:
        rows = self.ROWS(reversed([scenario_id for scenario_id, _ in REGISTRY]))
        ordered = [row.scenario_id for row in select(rows, _declared(), "test")]
        assert ordered == sorted(ordered)

    def test_an_undeclared_row_is_refused_on_either_side(self) -> None:
        rows = self.ROWS(["polymarket:never-sealed"])
        for partition in ("dev", "test"):
            with pytest.raises(UnknownScenario):
                select(rows, _declared(), partition)  # type: ignore[arg-type]


class TestTheGuard:
    def test_it_refuses_a_set_containing_one_test_scenario(self) -> None:
        declared = _declared()
        with pytest.raises(HeldOutViolation, match="1 of 41"):
            require_dev_only([*declared.dev, declared.test[0]], declared, what="a sweep")

    def test_it_refuses_a_set_that_is_entirely_test(self) -> None:
        declared = _declared()
        with pytest.raises(HeldOutViolation):
            require_dev_only(declared.test, declared, what="a sweep")

    def test_it_names_what_was_refused_and_where_dev_is(self) -> None:
        declared = _declared()
        with pytest.raises(HeldOutViolation) as caught:
            require_dev_only([declared.test[3]], declared, what="prompt sweep r7")
        message = str(caught.value)
        assert "prompt sweep r7" in message
        assert declared.test[3] in message
        assert "cascade eval split --ids dev" in message

    def test_it_refuses_an_id_nobody_declared(self) -> None:
        """Unknown is not "probably dev"."""
        with pytest.raises(HeldOutViolation, match="not in the declared split"):
            require_dev_only(["polymarket:never-sealed"], _declared(), what="a sweep")

    def test_it_passes_dev_and_returns_exactly_what_it_checked(self) -> None:
        declared = _declared()
        assert require_dev_only(
            [declared.dev[2], declared.dev[0], declared.dev[2]], declared, what="a sweep"
        ) == (declared.dev[0], declared.dev[2])

    def test_every_refusal_is_a_split_error(self) -> None:
        """One base class, so one clause at the CLI boundary maps them to 3."""
        assert issubclass(HeldOutViolation, SplitError)
        assert issubclass(UnknownScenario, SplitError)
        assert issubclass(SplitDeclarationMismatch, SplitError)


class TestUndeclaredConfigurations:
    DECLARED = frozenset({"C01", "C05", "S01", "B2_single_direct"})

    @pytest.mark.parametrize("partition", ["test", "all"])
    def test_a_tuning_variant_is_refused_off_dev(self, partition: str) -> None:
        with pytest.raises(HeldOutViolation, match="not a declared study configuration"):
            require_declared_config(
                "k9-try3", partition=partition, declared=self.DECLARED  # type: ignore[arg-type]
            )

    def test_a_tuning_variant_is_welcome_on_dev(self) -> None:
        require_declared_config("k9-try3", partition="dev", declared=self.DECLARED)

    @pytest.mark.parametrize("partition", ["dev", "test", "all"])
    def test_a_declared_configuration_is_scoreable_anywhere(self, partition: str) -> None:
        require_declared_config(
            "S01", partition=partition, declared=self.DECLARED  # type: ignore[arg-type]
        )


class TestInteractions:
    """The three keyed subsets, against each other. Reported, not hidden."""

    def _rows(self):
        ids = [scenario_id for scenario_id, _ in REGISTRY]
        cap = load_settings().ensemble.ablation_scenarios
        return interactions(
            _declared(),
            ablation_subsample=grid_scenarios(ids, cell=cell_by_id("C05"), salt=_salt(), cap=cap),
            recalibration_fit=[
                scenario_id
                for scenario_id in ids
                if recalibration_half(scenario_id, salt=_salt()) == "fit"
            ],
        )

    def test_the_overlaps_on_the_sealed_registry(self) -> None:
        dev, test = self._rows()
        assert (dev.partition, dev.n, dev.ablation_subsample) == ("dev", 40, 16)
        assert (test.partition, test.n, test.ablation_subsample) == ("test", 140, 74)
        assert (dev.recalibration_fit, dev.recalibration_held) == (18, 22)
        assert (test.recalibration_fit, test.recalibration_held) == (75, 65)

    def test_the_subsample_is_accounted_for_exactly(self) -> None:
        dev, test = self._rows()
        assert dev.ablation_subsample + test.ablation_subsample == 90
        assert dev.recalibration_fit + dev.recalibration_held == dev.n

    def test_the_split_is_independent_of_the_ablation_ranking(self) -> None:
        """Without the purpose string all 40 dev scenarios fall inside the
        ablation's 90 and the capped cells keep 50 test scenarios, not 74.
        Independence predicts 20; anything near 40 means the digest is shared."""
        dev, _ = self._rows()
        assert 12 <= dev.ablation_subsample <= 28

    def test_both_recalibration_halves_are_usable_inside_each_partition(self) -> None:
        for row in self._rows():
            assert row.recalibration_fit >= 2 and row.recalibration_held >= 2


class TestTheNewModulesArePure:
    """§4 "pure core, thin shell": no I/O, no clock, no generator of their own.

    The same import-level check `test_invariants.py` applies to the arbiter,
    applied to the modules that decide what is held out and what is tiered.
    """

    @pytest.mark.parametrize("name", ["split.py", "evidence.py", "supplementary.py"])
    def test_it_imports_nothing_that_reaches_outside_the_process(self, name: str) -> None:
        import ast

        from tests.unit.test_invariants import (
            IMPURE_CASCADE_MODULES,
            IMPURE_ROOTS,
            _imported_roots,
        )

        tree = ast.parse((repo_root() / "cascade" / "eval" / name).read_text(encoding="utf-8"))
        offenders = sorted(
            root
            for root in _imported_roots(tree)
            if root.split(".")[0] in IMPURE_ROOTS
            or any(root.startswith(prefix) for prefix in IMPURE_CASCADE_MODULES)
            or root in {"cascade.config", "cascade.eval.store", "numpy.random"}
        )
        assert offenders == []

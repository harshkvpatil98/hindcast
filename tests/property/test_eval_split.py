"""Properties of the dev/test split, for every registry rather than for ours.

`tests/unit/test_eval_split.py` pins the split of the sealed 180. These say
what stays true of *any* registry, which is what makes the pinned one an
instance of a rule rather than a lucky draw: exact size, a true partition, the
quota rule, indifference to input order -- and the stability argument the
module's docstring makes, stated as something Hypothesis can try to break.
"""

from __future__ import annotations

from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from cascade.eval.split import declare_split, domain_quotas, require_dev_only, split_score

DOMAINS = ("elections", "sports", "technology", "conflict", "labor")
SALT = "property-salt"


@st.composite
def registries(draw: st.DrawFn, min_size: int = 12, max_size: int = 70):
    """A registry and a feasible dev size: every domain keeps a test scenario."""
    count = draw(st.integers(min_value=min_size, max_value=max_size))
    ids = draw(
        st.lists(
            st.text(alphabet="abcdefghij0123456789:-", min_size=3, max_size=14),
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    pairs = [(scenario_id, draw(st.sampled_from(DOMAINS))) for scenario_id in ids]
    ceiling = len(pairs) - len({domain for _, domain in pairs})
    dev_size = draw(st.integers(min_value=1, max_value=max(1, ceiling - 1)))
    return pairs, dev_size


@given(registries())
@settings(max_examples=150, deadline=None)
def test_the_split_is_an_exact_partition(data: tuple[list[tuple[str, str]], int]) -> None:
    pairs, dev_size = data
    declared = declare_split(pairs, salt=SALT, dev_size=dev_size)
    assert len(declared.dev) == dev_size
    assert set(declared.dev).isdisjoint(declared.test)
    assert set(declared.dev) | set(declared.test) == {scenario_id for scenario_id, _ in pairs}


@given(registries(), st.randoms(use_true_random=False))
@settings(max_examples=100, deadline=None)
def test_the_split_ignores_the_order_it_was_given(
    data: tuple[list[tuple[str, str]], int], shuffler
) -> None:
    """A split that depended on load order would depend on a query plan."""
    pairs, dev_size = data
    shuffled = list(pairs)
    shuffler.shuffle(shuffled)
    assert declare_split(shuffled, salt=SALT, dev_size=dev_size) == declare_split(
        pairs, salt=SALT, dev_size=dev_size
    )


@given(registries())
@settings(max_examples=150, deadline=None)
def test_every_domain_is_within_one_of_its_share_and_keeps_a_test_scenario(
    data: tuple[list[tuple[str, str]], int],
) -> None:
    pairs, dev_size = data
    sizes = Counter(domain for _, domain in pairs)
    quotas = domain_quotas(sizes, dev_size=dev_size)
    assert sum(quotas.values()) == dev_size
    # The quota rule, in integers: floor(share) <= quota <= floor(share) + 1.
    # The upper half can only give when "test keeps one of each" had to pass a
    # saturated domain's seat along, so it is asserted when none is saturated.
    saturated = any(quotas[domain] + 1 >= sizes[domain] for domain in sorted(quotas))
    for domain, quota in sorted(quotas.items()):
        floor = dev_size * sizes[domain] // len(pairs)
        assert 0 <= quota < sizes[domain]
        assert quota >= floor
        if not saturated:
            assert quota <= floor + 1


def _prefix_lengths(pairs: list[tuple[str, str]], dev: set[str]) -> dict[str, int]:
    """Assert dev is a prefix of the per-id order in every domain; return lengths."""
    by_domain: dict[str, list[str]] = {}
    for scenario_id, domain in pairs:
        by_domain.setdefault(domain, []).append(scenario_id)
    lengths: dict[str, int] = {}
    for domain, members in sorted(by_domain.items()):
        ranked = sorted(members, key=lambda sid: (split_score(sid, salt=SALT), sid))
        flags = [scenario_id in dev for scenario_id in ranked]
        assert flags == sorted(flags, reverse=True), (
            f"in {domain!r} a dev scenario ranks below a test scenario: membership is not "
            "decided by the per-id score"
        )
        lengths[domain] = sum(flags)
    return lengths


@given(registries(), st.sampled_from(DOMAINS))
@settings(max_examples=200, deadline=None)
def test_adding_a_scenario_never_reorders_the_others(
    data: tuple[list[tuple[str, str]], int], domain: str
) -> None:
    """The reconciliation of exact counts with stability, as a property.

    Before and after an addition, dev is a prefix of **the same** per-id order
    in every domain. So an existing scenario can change sides only by the line
    moving past it: at most ``|quota change|`` of them per domain, plus one in
    the domain that gained the newcomer. A positional split -- a seeded shuffle
    of the list, "the first 40" -- fails this immediately, because inserting
    one element re-deals everyone after it.
    """
    pairs, dev_size = data
    newcomer = ("zz-the-newcomer", domain)
    before = declare_split(pairs, salt=SALT, dev_size=dev_size)
    after = declare_split([*pairs, newcomer], salt=SALT, dev_size=dev_size)

    lengths_before = _prefix_lengths(pairs, set(before.dev))
    lengths_after = _prefix_lengths([*pairs, newcomer], set(after.dev))

    domain_of = dict(pairs)
    moved = Counter(
        domain_of[scenario_id]
        for scenario_id, _ in pairs
        if (scenario_id in set(before.dev)) != (scenario_id in set(after.dev))
    )
    for name in sorted(lengths_after):
        allowed = abs(lengths_after[name] - lengths_before.get(name, 0)) + (name == domain)
        assert moved[name] <= allowed


@given(registries())
@settings(max_examples=100, deadline=None)
def test_a_scenario_s_partition_does_not_depend_on_which_others_are_scored(
    data: tuple[list[tuple[str, str]], int],
) -> None:
    """Looked up, never re-derived: whatever subset is asked about, each id
    gets the side the declaration gave it."""
    pairs, dev_size = data
    declared = declare_split(pairs, salt=SALT, dev_size=dev_size)
    for scenario_id, _ in pairs[::3]:
        assert declared.partition_of(scenario_id) == (
            "dev" if scenario_id in set(declared.dev) else "test"
        )


@given(registries())
@settings(max_examples=100, deadline=None)
def test_the_guard_passes_every_dev_subset_and_no_set_touching_test(
    data: tuple[list[tuple[str, str]], int],
) -> None:
    import pytest

    from cascade.eval.split import HeldOutViolation

    pairs, dev_size = data
    declared = declare_split(pairs, salt=SALT, dev_size=dev_size)
    assert require_dev_only(declared.dev, declared, what="property") == declared.dev
    for held_out in declared.test[:5]:
        with pytest.raises(HeldOutViolation):
            require_dev_only([*declared.dev, held_out], declared, what="property")

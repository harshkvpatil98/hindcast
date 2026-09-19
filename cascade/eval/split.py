"""The declared dev/test split. Pure: no I/O, no clock, no RNG, **no labels**.

Every choice made by looking at accuracy on the scenarios that produce the
headline inflates the headline: how many evidence chunks an agent sees, a
prompt edit, a blend weight. Before this module existed the only safe way to
tune Cascade was not to. The split makes tuning legitimate on one partition
(``dev``) and keeps the other (``test``) for the number the study reports.

It is declared **before a single forecast exists**, which is the only moment a
split can be declared without anyone being able to say it was chosen after
looking. Three things make that claim checkable rather than asserted:

* **No outcome can reach it.** :func:`declare_split` accepts scenario ids,
  domains, the study salt and a size. There is no parameter a label could be
  passed through, and a test asserts the signature stays that way.
* **It is reproducible from the salt.** Membership is decided by a keyed
  blake2b of the scenario id -- the house pattern of
  ``ablation.grid_scenarios`` and ``score.split_halves`` -- under its own
  purpose string, so the three subsets are statistically independent of each
  other instead of being three readings of one digest.
* **It is pinned.** :attr:`SplitDeclaration.sha256` digests the exact
  membership. ``eval.split_sha256`` in ``configs/base.yaml`` records it, and
  :func:`assert_declared` refuses any evaluation whose recomputed split
  differs. The commit that introduced the pin is the dated declaration.

Method: stratified hash rank
----------------------------

Each scenario gets one fixed 64-bit score, ``blake2b(purpose | id, key=salt)``.
Within each domain the scenarios are ordered by that score and the first
``q_d`` are ``dev``. The quotas ``q_d`` apportion ``dev_size`` across domains
in proportion to domain size by the largest-remainder rule in exact integer
arithmetic, ties going to the larger domain and then to the domain name.

Stratifying is legitimate here because a domain is a deterministic function
of the question text (``ledger.taxonomy.classify_domain``), fixed at M1 before
any outcome was joined to anything; it cannot carry information about a label
that the id does not. It is worth doing because a 40-scenario dev set drawn
without it has a real chance of holding two elections questions or twelve --
and a dev set that does not look like the test set tunes toward the wrong
population.

Exact counts versus stability
-----------------------------

A pure per-id threshold (``score < f``) is perfectly stable -- no scenario's
partition depends on any other scenario -- but its size is binomial: at
``f = 40/180`` the dev set lands anywhere from about 29 to 51 (two standard
deviations), and a small domain's share is a coin flip. A rank gives exact
counts and costs stability. This module takes the rank and bounds the cost, in
three ways:

1. **The order never changes.** A scenario's score depends on its own id
   alone, so adding or removing a scenario never reorders the others. In every
   domain ``dev`` is always a *prefix* of one fixed order; only the prefix
   length can move. A positional rule ("shuffle with a seeded RNG, take the
   first 40") has no such property: one insertion reshuffles everything.
2. **A change to the registry moves only boundary scenarios.** Within a
   domain, the existing scenarios that change partition number at most the
   change in that domain's quota, plus one in the domain that gained the
   newcomer. Property-tested. Measured on the sealed 180: adding one scenario
   moves between 1 and 4 of the 180, removing one moves 0 to 2. It is not
   zero, and cannot be: a fixed total re-apportioned over a changed ``n``
   shifts quotas, and this registry sits on exact ties (five domains share one
   remainder) that any change in ``n`` re-breaks. A positional split would move
   about 60.
3. **The split is never recomputed over a subset.** A rank over "the scenarios
   that happen to be scored" is a different partition from a rank over the
   registry, and the difference is a contamination channel. The only way to
   learn a scenario's partition is to ask a :class:`SplitDeclaration`, which is
   built from the whole sealed registry; subsets are *intersected* with it,
   and an id the declaration has never seen raises rather than being assigned.

A moved boundary is still a moved boundary: if the registry ever changes, a
scenario that was tuned on may land in ``test``. That is what the pin is for.
A changed registry changes :attr:`SplitDeclaration.sha256`, every evaluation
path exits 3, and the correct response is a new salt and a new declaration --
never to re-pin and carry on with forecasts that were tuned under the old one.

How the three keyed subsets interact
------------------------------------

* **Ablation subsample** (``ablation.grid_scenarios``, 90 of 180). Drawn from
  an independent digest, so roughly ``90 x dev/n`` of it falls in ``dev``. The
  capped cells therefore pair with the headline on fewer ``test`` scenarios
  than 90, and every interval on a capped cell is wider for it.
  :func:`interactions` reports the exact overlap; it is printed, not hidden.
* **Recalibration halves** (``score.split_halves``). Isotonic recalibration is
  fitted on one half and scored on the other half **of whatever set it is
  handed**. The report hands it one partition at a time, so a fit never
  crosses the dev/test boundary: the ``test`` figure is fitted on test
  scenarios only, and no dev forecast or dev label can move it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cascade.canonical import canonical_json
from cascade.eval.exclusions import ExcludedScenario, exclusions

__all__ = [
    "PARTITIONS",
    "SPLIT_PURPOSE",
    "DomainAllocation",
    "ExcludedFromScoring",
    "HeldOutViolation",
    "Partition",
    "PartitionInteraction",
    "SplitDeclaration",
    "SplitDeclarationMismatch",
    "SplitError",
    "UnknownScenario",
    "assert_declared",
    "declare_split",
    "declare_study_split",
    "domain_quotas",
    "interactions",
    "require_declared_config",
    "require_dev_only",
    "select",
    "split_score",
]

SPLIT_PURPOSE = "cascade.eval.split/dev-test/v1"
"""Domain separation for the split's digest.

``grid_scenarios`` and ``split_halves`` both hash the bare scenario id under
the study salt. Hashing the bare id a third time would rank scenarios exactly
as the ablation subsample does, so the lowest-ranked -- dev -- would sit inside
the ablation's 90: measured on the sealed registry, all 40 of them. The capped
cells would then keep 50 test scenarios instead of about 70, for no reason but
a shared digest. The purpose string makes this an independent draw, and a test
holds the overlap to what independence predicts.

Versioned so that a future, deliberately different split is a visible edit to
a constant and a changed pin, rather than a quiet change of behaviour.
"""

Partition = Literal["all", "dev", "test"]
PARTITIONS: tuple[Partition, ...] = ("all", "dev", "test")


class SplitError(RuntimeError):
    """Base for every refusal this module makes. The CLI maps it to exit 3."""


class HeldOutViolation(SplitError):
    """A tuning path was handed something from the held-out partition."""


class UnknownScenario(SplitError):
    """A scenario id the declaration has never seen was asked for a partition."""


class ExcludedFromScoring(SplitError):
    """A scenario declared unscoreable was asked for its partition."""


class SplitDeclarationMismatch(SplitError):
    """The recomputed split is not the one that was declared and pinned."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DomainAllocation(_Frozen):
    """One domain's share of the split. Counts only; nothing measured."""

    domain: str
    n: int
    dev: int
    test: int


class PartitionInteraction(_Frozen):
    """How one partition overlaps the study's two other keyed subsets."""

    partition: Literal["dev", "test"]
    n: int
    ablation_subsample: int
    recalibration_fit: int
    recalibration_held: int


class SplitDeclaration(_Frozen):
    """The dev/test partition of one sealed registry, and its fingerprint.

    Built only by :func:`declare_split`. Carries ids and counts and nothing
    else: it is safe to print, to commit and to hand to the simulation role,
    because there is no field an outcome could occupy.
    """

    purpose: str
    dev_size: int
    dev: tuple[str, ...]
    test: tuple[str, ...]
    domains: tuple[DomainAllocation, ...]
    sha256: str
    excluded: tuple[ExcludedScenario, ...] = ()
    """Sealed scenarios on neither side: declared unscoreable before any
    forecast (``cascade/eval/exclusions.py``). Part of the fingerprint."""

    @property
    def excluded_ids(self) -> tuple[str, ...]:
        return tuple(item.scenario_id for item in self.excluded)

    @property
    def n(self) -> int:
        return len(self.dev) + len(self.test)

    def ids(self, partition: Partition) -> tuple[str, ...]:
        """The sorted ids of one partition; ``all`` is their union."""
        if partition == "dev":
            return self.dev
        if partition == "test":
            return self.test
        return tuple(sorted((*self.dev, *self.test)))

    def partition_of(self, scenario_id: str) -> Literal["dev", "test"]:
        """Which side a declared scenario is on; raises on an undeclared one.

        Preserves the rule that a partition is looked up, never derived: an id
        outside the declared registry has no partition, and guessing one --
        in either direction -- is how a tuned scenario ends up scored as held
        out.
        """
        if scenario_id in set(self.dev):
            return "dev"
        if scenario_id in set(self.test):
            return "test"
        if scenario_id in set(self.excluded_ids):
            raise ExcludedFromScoring(
                f"scenario {scenario_id!r} is sealed but excluded from scoring "
                f"({dict((e.scenario_id, e.reason) for e in self.excluded)[scenario_id]}); "
                "it is on neither side of the split and may be neither tuned on nor scored."
            )
        raise UnknownScenario(
            f"scenario {scenario_id!r} is not in the declared split "
            f"({self.sha256[:16]}..., {self.n} scenarios). A partition is looked up "
            "against the sealed registry, never assigned to a newcomer on the fly."
        )


def split_score(scenario_id: str, *, salt: str) -> bytes:
    """The fixed 8-byte score that orders one scenario within its domain.

    Preserves per-id stability: the score is a function of this id, the salt
    and :data:`SPLIT_PURPOSE` alone, so no other scenario's presence, order or
    outcome can change it.
    """
    return hashlib.blake2b(
        f"{SPLIT_PURPOSE}|{scenario_id}".encode(),
        digest_size=8,
        key=salt.encode("utf-8"),
    ).digest()


def domain_quotas(sizes: Mapping[str, int], *, dev_size: int) -> dict[str, int]:
    """Apportion ``dev_size`` across domains by largest remainder. Exact.

    Preserves three things. The quotas sum to ``dev_size`` exactly. Each is
    within one of its proportional share, ``dev_size * n_d / n`` -- the quota
    rule, which is the domain-balance tolerance the tests assert. And every
    domain keeps at least one scenario in ``test``: the headline is a claim
    about the test partition, and a domain tuned on in its entirety would be
    a domain the headline silently says nothing about.

    Integer arithmetic throughout. ``29 * 40 / 180`` and ``11 * 40 / 180`` have
    the same fractional part exactly, and a float comparison would break that
    tie by rounding noise. Ties go to the larger domain, where one extra dev
    scenario distorts the domain's own ratio least, and then to the domain
    name -- both outcome-independent.
    """
    total = sum(sizes.values())
    if any(count <= 0 for count in sorted(sizes.values())):
        raise ValueError("every domain must hold at least one scenario")
    ceiling = total - len(sizes)
    if not 0 < dev_size <= ceiling:
        raise ValueError(
            f"dev_size must lie in [1, {ceiling}] for {total} scenarios in "
            f"{len(sizes)} domains (test keeps one of each); got {dev_size}"
        )
    quotas = {domain: dev_size * count // total for domain, count in sorted(sizes.items())}
    remainders = {domain: dev_size * count % total for domain, count in sorted(sizes.items())}
    leftover = dev_size - sum(quotas.values())
    order = sorted(sizes, key=lambda domain: (-remainders[domain], -sizes[domain], domain))
    # Two passes at most: the second only runs when the first had to skip a
    # domain whose next scenario would have been its last.
    while leftover > 0:
        granted = 0
        for domain in order:
            if leftover == 0:
                break
            if quotas[domain] + 1 < sizes[domain]:
                quotas[domain] += 1
                leftover -= 1
                granted += 1
        if granted == 0:  # pragma: no cover - excluded by the ceiling check
            raise ValueError("dev_size cannot be apportioned without emptying a domain's test side")
    return quotas


def declare_split(
    scenarios: Sequence[tuple[str, str]], *, salt: str, dev_size: int
) -> SplitDeclaration:
    """Partition ``(scenario_id, domain)`` pairs into dev and test.

    Preserves outcome-independence **by construction**: ids, domains, the salt
    and a size are the only inputs, so there is no argument through which a
    label, a forecast or an error could influence membership. Also preserves
    order-independence (the input is sorted before anything is ranked) and
    exact size (``len(dev) == dev_size``).

    Must be called with the **whole sealed registry**. Calling it on a subset
    yields a different partition -- see the module docstring -- which is why
    every other function here takes a declaration rather than re-deriving one.
    """
    ordered = sorted(scenarios)
    ids = [scenario_id for scenario_id, _ in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate scenario ids: a scenario cannot be on both sides of a split")
    by_domain: dict[str, list[str]] = {}
    for scenario_id, domain in ordered:
        by_domain.setdefault(domain, []).append(scenario_id)
    quotas = domain_quotas(
        {domain: len(members) for domain, members in sorted(by_domain.items())},
        dev_size=dev_size,
    )

    dev: list[str] = []
    test: list[str] = []
    allocations: list[DomainAllocation] = []
    for domain, members in sorted(by_domain.items()):
        # The id is a tie-break that can never fire on a 64-bit digest of
        # distinct ids short of a collision, and makes the order total if it does.
        ranked = sorted(
            members, key=lambda scenario_id: (split_score(scenario_id, salt=salt), scenario_id)
        )
        quota = quotas[domain]
        dev.extend(ranked[:quota])
        test.extend(ranked[quota:])
        allocations.append(
            DomainAllocation(domain=domain, n=len(members), dev=quota, test=len(members) - quota)
        )
    dev_ids = tuple(sorted(dev))
    test_ids = tuple(sorted(test))
    return SplitDeclaration(
        purpose=SPLIT_PURPOSE,
        dev_size=dev_size,
        dev=dev_ids,
        test=test_ids,
        domains=tuple(allocations),
        sha256=_fingerprint(dev_ids, test_ids),
    )


def _fingerprint(
    dev: Sequence[str],
    test: Sequence[str],
    excluded: Sequence[ExcludedScenario] = (),
) -> str:
    """sha256 of the exact membership, over the system's one canonical JSON.

    The salt is deliberately not an input: the fingerprint identifies *which
    scenarios are held out*, and two routes to the same membership are the
    same declaration. Excluded scenarios and their reasons are in it when
    there are any, so widening the exclusion rule after the pin changes the
    fingerprint and is refused like any other re-drawn split.
    """
    body: dict[str, object] = {"purpose": SPLIT_PURPOSE, "dev": list(dev), "test": list(test)}
    if excluded:
        body["excluded"] = [[item.scenario_id, item.reason] for item in excluded]
    payload = canonical_json(body)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def declare_study_split(
    scenarios: Sequence[tuple[str, str, str]], *, salt: str, dev_size: int
) -> SplitDeclaration:
    """The study's split: ``(scenario_id, domain, question)`` for the whole
    sealed registry, stand-ins excluded first, the rest split by
    :func:`declare_split`.

    Preserves outcome-independence by construction, as :func:`declare_split`
    does: ids, domains, wording, a salt and a size are the only inputs.
    Exclusion happens before quotas are apportioned, so the dev partition is
    ``dev_size`` real questions rather than ``dev_size`` minus however many
    stand-ins the hash happened to draw.
    """
    excluded = exclusions([(scenario_id, question) for scenario_id, _, question in scenarios])
    gone = {item.scenario_id for item in excluded}
    base = declare_split(
        [(scenario_id, domain) for scenario_id, domain, _ in scenarios if scenario_id not in gone],
        salt=salt,
        dev_size=dev_size,
    )
    return base.model_copy(
        update={"excluded": excluded, "sha256": _fingerprint(base.dev, base.test, excluded)}
    )


def assert_declared(declaration: SplitDeclaration, *, pinned_sha256: str | None) -> None:
    """Refuse a split that is not the one that was declared.

    Preserves the frozen-split discipline of §1.3 one level down: the registry
    is re-hashed against its seal before a label is read, and the partition is
    re-derived against its pin before a label is *used*. A changed salt, size,
    purpose string or registry all surface here, before any number exists.

    ``None`` means no pin has been recorded, which is refused rather than
    waved through: an unpinned split is one that could be re-drawn after
    looking, and nothing downstream could tell.
    """
    if pinned_sha256 is None:
        raise SplitDeclarationMismatch(
            "no dev/test split is pinned (eval.split_sha256 is unset). Run "
            "`cascade eval split`, record the sha256 it prints in configs/base.yaml, "
            "and commit it before any forecast exists. Computed now: "
            f"{declaration.sha256}"
        )
    if declaration.sha256 != pinned_sha256:
        raise SplitDeclarationMismatch(
            f"the dev/test split recomputed from the registry is {declaration.sha256} "
            f"but the declared split is {pinned_sha256}. The salt, eval.dev_scenarios, "
            "the split's purpose string or the registry has changed since the "
            "declaration. Do not re-pin: a split re-drawn after forecasts exist is "
            "a split chosen after looking."
        )


class _HasScenarioId(Protocol):
    @property
    def scenario_id(self) -> str: ...


def select[Item: _HasScenarioId](
    items: Iterable[Item], declaration: SplitDeclaration, partition: Partition
) -> tuple[Item, ...]:
    """The items whose scenario lies in ``partition``, in scenario-id order.

    Preserves intersect-never-resplit: membership comes from the declaration,
    so the partition of a scored subset is the declared partition restricted to
    it, whatever else is or is not in ``items``. An undeclared id raises under
    ``dev`` and ``test``; ``all`` is the identity on membership and only sorts.
    """
    excluded = set(declaration.excluded_ids)
    ordered = [
        item
        for item in sorted(items, key=lambda item: item.scenario_id)
        if item.scenario_id not in excluded
    ]
    if partition == "all":
        return tuple(ordered)
    wanted = set(declaration.ids(partition))
    known = wanted | set(declaration.ids("test" if partition == "dev" else "dev"))
    for item in ordered:
        if item.scenario_id not in known:
            declaration.partition_of(item.scenario_id)  # raises UnknownScenario
    return tuple(item for item in ordered if item.scenario_id in wanted)


def require_dev_only(
    scenario_ids: Iterable[str], declaration: SplitDeclaration, *, what: str
) -> tuple[str, ...]:
    """Refuse a scenario set that touches the held-out partition.

    The guard every tuning entry point calls before it looks at anything.
    Preserves the one rule that makes a tuned system's headline mean what it
    says: **no decision is ever informed by a test scenario.** Returns the
    sorted, de-duplicated ids so a caller can only proceed with what was
    checked.

    An id the declaration does not know is refused as well. Unknown is not
    "probably dev"; the safe reading of a scenario nobody declared is that it
    is held out.
    """
    requested = sorted(set(scenario_ids))
    dev = set(declaration.dev)
    test = set(declaration.test)
    excluded = set(declaration.excluded_ids)
    held_out = [scenario_id for scenario_id in requested if scenario_id in test]
    dropped = [scenario_id for scenario_id in requested if scenario_id in excluded]
    unknown = [scenario_id for scenario_id in requested if scenario_id not in dev | test | excluded]
    if held_out or dropped or unknown:
        shown = ", ".join((held_out + dropped + unknown)[:5])
        raise HeldOutViolation(
            f"{what} refused: {len(held_out)} of {len(requested)} scenario(s) are in the "
            f"held-out test partition, {len(dropped)} are excluded from scoring and "
            f"{len(unknown)} are not in the declared split (first: {shown}). Tuning is only "
            f"ever legitimate on dev ({len(declaration.dev)} scenarios; "
            "`cascade eval split --ids dev` lists them)."
        )
    return tuple(requested)


def require_declared_config(
    config_id: str, *, partition: Partition, declared: Collection[str]
) -> None:
    """Refuse to score an undeclared configuration anywhere but on dev.

    Preserves the distinction between a *study configuration* -- named in code
    before it was run: the twelve cells, the baselines, the supplementary
    cells -- and a *tuning variant*, which is anything else with forecasts
    behind it. A variant exists to be compared and discarded, and the moment
    its test-partition score is visible, choosing among variants is choosing
    on the headline's own scenarios. Declaring a variant is a commit; that is
    the price of looking.
    """
    if partition == "dev" or config_id in declared:
        return
    raise HeldOutViolation(
        f"config {config_id!r} is not a declared study configuration, so it may only "
        f"be scored on the dev partition (asked for {partition!r}). Declared: "
        f"{', '.join(sorted(declared))}. To report a new configuration on test, "
        "declare it in code first -- see cascade/eval/supplementary.py."
    )


def interactions(
    declaration: SplitDeclaration,
    *,
    ablation_subsample: Collection[str],
    recalibration_fit: Collection[str],
) -> tuple[PartitionInteraction, ...]:
    """Count how each partition overlaps the other two keyed subsets.

    Preserves visibility: the ablation subsample and the recalibration halves
    are drawn independently of this split, so their overlap with it is a
    random quantity that determines the paired ``n`` of every capped-cell
    comparison and the fitting ``n`` of every recalibrated figure. It is
    computed from ids alone and printed in the report.
    """
    subsample = set(ablation_subsample)
    fit = set(recalibration_fit)
    out: list[PartitionInteraction] = []
    for partition in ("dev", "test"):
        members = declaration.ids(partition)
        in_fit = sum(1 for scenario_id in members if scenario_id in fit)
        out.append(
            PartitionInteraction(
                partition=partition,
                n=len(members),
                ablation_subsample=sum(1 for scenario_id in members if scenario_id in subsample),
                recalibration_fit=in_fit,
                recalibration_held=len(members) - in_fit,
            )
        )
    return tuple(out)

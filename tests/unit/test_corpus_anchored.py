"""Cutoff-anchored ingest planning (ADR-0036). Pure, so tested without a network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cascade.corpus.anchored import CrawlFile, parse_listing, plan_anchored, unit_key, worth


def listing(year: int, month: int, *, days: int = 28, per_day: int = 4) -> list[CrawlFile]:
    """A synthetic month: ``per_day`` files a day, evenly spaced, named as CC-NEWS names them."""
    paths = []
    for day in range(1, days + 1):
        for slot in range(per_day):
            stamp = datetime(year, month, day, slot * (24 // per_day), 5, 0)
            paths.append(
                f"crawl-data/CC-NEWS/{year:04d}/{month:02d}/CC-NEWS-{stamp:%Y%m%d%H%M%S}-{len(paths):05d}.warc.gz"
            )
    return parse_listing(f"{year:04d}/{month:02d}", paths)


def at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def plan(cutoffs: list[datetime], listings: dict, **kwargs: object) -> list[str]:  # type: ignore[type-arg]
    options = {
        "done": (),
        "max_files": 5,
        "half_life_days": 14.0,
        "lookback_days": 540.0,
        "floor_days": 30.0,
    }
    options.update(kwargs)
    return plan_anchored(cutoffs, listings, **options)  # type: ignore[arg-type]


MARCH = {"2026/03": listing(2026, 3)}


def file_of(key: str, listings: dict) -> CrawlFile:  # type: ignore[type-arg]
    return next(f for files in listings.values() for f in files if unit_key(f) == key)


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


def test_a_listing_yields_crawl_spans_indexed_as_served() -> None:
    files = MARCH["2026/03"]
    assert [f.index for f in files] == list(range(len(files)))
    assert files[0].crawled_from == datetime(2026, 3, 1, 0, 5, tzinfo=UTC)
    # A file spans until the next one starts.
    assert files[0].crawled_until == files[1].crawled_from


def test_an_unrecognised_file_name_is_an_error_not_a_skipped_entry() -> None:
    with pytest.raises(ValueError, match="unrecognised CC-NEWS file name"):
        parse_listing("2026/03", ["crawl-data/CC-NEWS/2026/03/something-else.warc.gz"])


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def test_the_first_pick_is_the_last_file_before_the_cutoff() -> None:
    cutoff = at(2026, 3, 25)
    first = file_of(plan([cutoff], MARCH, max_files=1)[0], MARCH)
    assert first.crawled_until <= cutoff
    later = [f for f in MARCH["2026/03"] if first.crawled_until < f.crawled_until <= cutoff]
    assert later == [], "a file closer to the cutoff was available and admissible"


def test_nothing_crawled_after_every_cutoff_is_ever_planned() -> None:
    cutoff = at(2026, 3, 10)
    planned = [file_of(key, MARCH) for key in plan([cutoff], MARCH, max_files=50)]
    assert planned, "the plan must not be empty"
    assert all(f.crawled_until <= cutoff for f in planned)


def test_files_beyond_the_lookback_are_worth_nothing() -> None:
    listings = {"2024/01": listing(2024, 1)}
    assert plan([at(2026, 3, 25)], listings, lookback_days=540.0) == []


def test_equity_decides_how_soon_a_lone_scenario_is_served() -> None:
    """Every scenario is 1/180 of the Brier, and one file serves a whole cluster.

    At equity 1 (log utility) a lone scenario beside a thirty-scenario cluster
    waits until the cluster is saturated; a higher equity serves it sooner. The
    trade is real and is the caller's to make, which is why it is a parameter.
    """
    listings = {"2026/03": listing(2026, 3), "2022/07": listing(2022, 7)}
    cluster = [at(2026, 3, 20 + i % 5, hour=i % 24) for i in range(30)]
    cutoffs = [*cluster, at(2022, 7, 20)]

    def first_served(equity: float) -> int:
        months = [
            k.split("#")[0]
            for k in plan(cutoffs, listings, max_files=40, equity=equity, floor_min_rescued=10_000)
        ]
        return months.index("2022/07") + 1

    assert plan(cutoffs, listings, max_files=1)[0].startswith(
        "2026/03"
    ), "thirty outweigh one first"
    assert first_served(3.0) < first_served(2.0) < first_served(1.0)
    assert first_served(2.0) <= 12, "at equity 2 the lone scenario is served within a dozen files"


def test_equity_below_log_utility_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        plan([at(2026, 3, 25)], MARCH, equity=0.5)


def test_what_is_already_ingested_counts_so_a_replan_does_not_repeat_it() -> None:
    cutoffs = [at(2026, 3, 25)]
    first = plan(cutoffs, MARCH, max_files=1)
    resumed = plan(cutoffs, MARCH, done=first, max_files=3)
    assert first[0] not in resumed
    fresh = plan(cutoffs, MARCH, max_files=4)
    assert fresh == [*first, *resumed], "re-planning after an interruption continues the same plan"


def test_the_plan_does_not_depend_on_the_order_things_arrive_in() -> None:
    cutoffs = [at(2026, 3, 25), at(2026, 3, 9), at(2026, 3, 17)]
    shuffled = {"2026/03": list(reversed(MARCH["2026/03"]))}
    assert plan(cutoffs, MARCH) == plan(list(reversed(cutoffs)), shuffled)


def test_the_budget_is_respected_and_zero_is_allowed() -> None:
    cutoffs = [at(2026, 3, 25)]
    assert len(plan(cutoffs, MARCH, max_files=3)) == 3
    assert plan(cutoffs, MARCH, max_files=0) == []
    with pytest.raises(ValueError, match="negative"):
        plan(cutoffs, MARCH, max_files=-1)


def test_a_file_is_worth_half_at_the_half_weight_distance_and_nothing_when_inadmissible() -> None:
    # The synthetic files are cut at five past the hour.
    five = timedelta(minutes=5)
    cutoff = at(2026, 3, 27) + five
    files = MARCH["2026/03"]
    ends_at = lambda when: next(f for f in files if f.crawled_until == when)  # noqa: E731
    kwargs = {"half_life_days": 14.0, "lookback_days": 540.0}
    assert worth(ends_at(at(2026, 3, 27, hour=12) + five), cutoff, **kwargs) == pytest.approx(
        1.0, abs=0.01
    )
    assert worth(ends_at(at(2026, 3, 13, hour=12) + five), cutoff, **kwargs) == pytest.approx(
        0.5, abs=0.01
    )
    # Crawled after the cutoff: nothing, however close.
    assert worth(ends_at(at(2026, 3, 27, hour=18) + five), cutoff, **kwargs) == 0.0
    # Beyond the lookback: nothing.
    assert (
        worth(
            ends_at(at(2026, 3, 13, hour=12) + five),
            cutoff,
            half_life_days=14.0,
            lookback_days=7.0,
        )
        == 0.0
    )


# ---------------------------------------------------------------------------
# The floor (phase 1)
# ---------------------------------------------------------------------------

TWO_MONTHS = {"2026/03": listing(2026, 3), "2026/06": listing(2026, 6)}


def test_the_floor_comes_first_and_takes_the_file_that_rescues_the_most() -> None:
    """Five scenarios with nothing from their last month outrank closeness for anyone."""
    served = [at(2026, 3, 25)]
    unserved = [at(2026, 6, 10 + i) for i in range(5)]
    already = plan(served, TWO_MONTHS, max_files=1)  # March is served; June is not
    first = file_of(
        plan([*served, *unserved], TWO_MONTHS, done=already, max_files=1)[0], TWO_MONTHS
    )
    assert first.month == "2026/06"
    assert all(0 <= (c - first.crawled_until).days <= 30 for c in unserved), "one file, all five"


def test_the_floor_does_not_spend_a_file_on_a_single_scenario() -> None:
    """One file is ~5% of the budget; a lone scenario is 0.6% of the study."""
    cluster = [at(2026, 3, 20 + i % 5, hour=i) for i in range(12)]
    loner = at(2026, 6, 20)
    with_floor = plan([*cluster, loner], TWO_MONTHS, max_files=3, floor_min_rescued=2)
    assert with_floor[0].startswith("2026/03"), "the cluster is rescued first"
    rescued_alone = plan([*cluster, loner], TWO_MONTHS, max_files=3, floor_min_rescued=1)
    assert any(k.startswith("2026/06") for k in rescued_alone[:2]), "at 1, it would have been"


def test_floor_parameters_are_validated() -> None:
    with pytest.raises(ValueError, match="floor_days"):
        plan([at(2026, 3, 25)], MARCH, floor_days=0.0)
    with pytest.raises(ValueError, match="floor_min_rescued"):
        plan([at(2026, 3, 25)], MARCH, floor_min_rescued=0)


# ---------------------------------------------------------------------------
# Two tests added after mutants survived
# ---------------------------------------------------------------------------


def test_the_weight_is_a_true_half_life_not_a_heavy_tail() -> None:
    """At one half-life both candidate formulas give 0.5; at two they part.

    The hyperbolic 1/(1+d/h) gives 1/3 at two half-lives and still 13% at
    ninety days, which is how fifteen stale files once added up to "well
    served" and the plan skipped the four most recent months.
    """
    five = timedelta(minutes=5)
    cutoff = at(2026, 3, 29) + five
    two_half_lives = next(f for f in MARCH["2026/03"] if f.crawled_until == at(2026, 3, 1) + five)
    value = worth(two_half_lives, cutoff, half_life_days=14.0, lookback_days=540.0)
    assert value == pytest.approx(0.25, abs=0.005)


def test_evidence_already_held_changes_what_is_worth_fetching_next() -> None:
    """Two scenarios in different months, so neither file serves the other.

    With nothing held the March file wins the tie on its key. Once March's
    scenario holds a file, its next one is worth far less than June's first --
    unless held evidence is ignored, in which case March is picked again.
    """
    cutoffs = [at(2026, 3, 25), at(2026, 6, 20)]
    closeness_only = {"floor_min_rescued": 10_000}
    first = plan(cutoffs, TWO_MONTHS, max_files=1, **closeness_only)
    assert first[0].startswith("2026/03")
    second = plan(cutoffs, TWO_MONTHS, done=first, max_files=1, **closeness_only)
    assert second[0].startswith("2026/06")

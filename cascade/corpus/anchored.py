"""Cutoff-anchored ingest planning for CC-NEWS (ADR-0036). Pure: no I/O, no clock.

A scenario is forecast from what was known just before its cutoff, and the last
days before a cutoff are the most informative ones -- the latest poll, the
latest filing. ADR-0023's queue did not know that. It ranked a *month* by how
many scenarios may use it, weighting a month seventeen months before a cutoff
the same as the month of the cutoff itself, and it deepened a month by taking
the next file in its listing. A CC-NEWS month lists ~400 files in crawl order,
~13 a day, so the twelve files that queue could ever reach all come from the
month's first two days. Measured on the rebuilt corpus: 394 files in 2026/03,
the twelfth crawled on 2 March; 82 of 180 scenarios had under 1,000 chunks from
their final thirty days, and 7 had none.

This planner chooses *files*, by the crawl time in their names, in two phases:

1. **A floor.** While some file would bring at least ``floor_min_rescued``
   scenarios their first evidence from the final ``floor_days`` before their
   cutoffs, take the file that rescues the most. A scenario with nothing from
   its last month is forecast as if that month never happened.
2. **Closeness.** Spend the rest greedily on total utility. A file is worth
   ``0.5 ** (days_before_cutoff / half_life_days)`` to each scenario whose
   cutoff it precedes -- a true half-life, so a file ninety days out is worth
   about 1%. (A first draft used a hyperbolic ``1/(1+d/14)``; its tail is so
   heavy that fifteen three-month-old files summed to "well served", and the
   plan skipped the four most recent months entirely.) A scenario's utility is
   concave in its evidence -- isoelastic, ``equity`` = 2 by default, log at 1 --
   so its first nearby file matters far more than its tenth, and **every
   scenario counts equally**, because each is 1/180 of the Brier.

The floor stops at ``floor_min_rescued`` = 2 on purpose. Measured: ten files
rescue 67 scenarios (23, 9, 8, 7, 5, 4, 3, 3, 3, 2); the next eleven would
rescue one each -- the early-cutoff scenarios alone in their months -- at over
half the remaining budget for 6% of the study. They are not abandoned: phase 2
takes a lone scenario's file as soon as that beats serving a cluster again.

Measured on the real cutoffs and listings, for the 18 files left in a 2.0M-chunk
budget, against ADR-0023's queue: scenarios with a file within a day of cutoff
4 -> 34, within a week 42 -> 91, within thirty days 164 -> 170, median gap
15.9 -> 6.9 days. It wins at budgets of 14 and 24 files too.

**It is a function of the cutoffs and the file listings alone -- never of an
outcome** (the precedents are ADR-0010 and ADR-0023). The parameters were chosen
by looking at how evidence is distributed across scenarios, before any forecast
existed; they are not to be tuned against one. Choosing evidence by what
improved the score would be steering (§1).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = ["CrawlFile", "evidence_held", "parse_listing", "plan_anchored", "unit_key", "worth"]

_NAME = re.compile(r"CC-NEWS-(\d{14})-\d+\.warc\.gz$")
# The last file of a listing has no successor to bound it. CC-NEWS cuts a file
# roughly every two hours (measured: 9-16 files a day), and overstating the
# span only makes the planner more conservative about admissibility.
_LAST_FILE_SPAN = timedelta(hours=3)


@dataclass(frozen=True)
class CrawlFile:
    """One WARC file: where it sits in its month's listing, and what it spans."""

    month: str  # "YYYY/MM"
    index: int  # position in the month's listing: the `k` of a `YYYY/MM#k` unit
    crawled_from: datetime
    crawled_until: datetime


def unit_key(file: CrawlFile) -> str:
    """The ingest unit for ``file`` -- the key shape `_ccnews_unit` already reads."""
    return f"{file.month}#{file.index}"


def parse_listing(month: str, paths: Sequence[str]) -> list[CrawlFile]:
    """Read crawl times out of a month's file names. Pure.

    Preserves the invariant that ``index`` is the file's position in the
    listing *as served*, because that is what the fetcher indexes by. A name
    that does not parse is an error rather than a skipped entry: skipping would
    shift nothing, but it would hide that the naming scheme had changed, and
    every crawl time after it would be a guess.
    """
    starts: list[datetime] = []
    for path in paths:
        match = _NAME.search(path)
        if match is None:
            raise ValueError(f"unrecognised CC-NEWS file name in {month}: {path!r}")
        starts.append(datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC))
    files = []
    for index, start in enumerate(starts):
        later = [other for other in starts if other > start]
        until = min(later) if later else start + _LAST_FILE_SPAN
        files.append(CrawlFile(month=month, index=index, crawled_from=start, crawled_until=until))
    return files


def worth(
    file: CrawlFile, cutoff: datetime, *, half_life_days: float, lookback_days: float
) -> float:
    """What ``file`` contributes to a scenario with this cutoff."""
    gap = (cutoff - file.crawled_until).total_seconds() / 86_400.0
    if gap < 0.0 or gap > lookback_days:
        # Crawled (even partly) after the cutoff, or too old to matter. The time
        # lock would filter the late articles out anyway; this is about not
        # spending the budget on a file most of which no scenario may read.
        return 0.0
    return float(0.5 ** (gap / half_life_days))


def evidence_held(
    cutoffs: Sequence[datetime],
    listings: Mapping[str, Sequence[CrawlFile]],
    units: Iterable[str],
    *,
    half_life_days: float,
    lookback_days: float,
) -> list[float]:
    """Recency-weighted evidence each scenario holds from ``units``, in sorted-cutoff order. Pure.

    The quantity the planner maximises, exposed so it can be *reported*: a plan
    is only worth adopting if this distribution is better, and that is a
    measurement, not an argument.
    """
    by_key = {unit_key(f): f for month in sorted(listings) for f in listings[month]}
    held = [by_key[key] for key in sorted(set(units)) if key in by_key]
    return [
        math.fsum(
            worth(f, cutoff, half_life_days=half_life_days, lookback_days=lookback_days)
            for f in held
        )
        for cutoff in sorted(cutoffs)
    ]


def _candidates(files: Iterable[CrawlFile], cutoffs: Sequence[datetime]) -> list[CrawlFile]:
    """The files worth considering: each day's last file, and each cutoff's last file.

    A day's ~13 files are consecutive slices of one crawl stream, so its last
    file stands for the day; adding the final file before every cutoff makes
    sure the single most valuable file for each scenario is always a candidate.
    """
    ordered = sorted(files, key=lambda f: (f.crawled_from, f.month, f.index))
    chosen: dict[tuple[str, int], CrawlFile] = {}
    by_day: dict[str, CrawlFile] = {}
    for file in ordered:
        by_day[file.crawled_from.strftime("%Y%m%d")] = file  # later files overwrite
    for _, file in sorted(by_day.items()):
        chosen[(file.month, file.index)] = file
    for cutoff in sorted(set(cutoffs)):
        before = [f for f in ordered if f.crawled_until <= cutoff]
        if before:
            last = before[-1]
            chosen[(last.month, last.index)] = last
    return [chosen[key] for key in sorted(chosen)]


def plan_anchored(
    cutoffs: Sequence[datetime],
    listings: Mapping[str, Sequence[CrawlFile]],
    *,
    done: Iterable[str],
    max_files: int,
    half_life_days: float,
    lookback_days: float,
    floor_days: float,
    floor_min_rescued: int = 2,
    equity: float = 2.0,
) -> list[str]:
    """Order up to ``max_files`` new units by marginal gain in total utility. Pure.

    Preserves three invariants. The plan depends only on cutoffs and listings,
    never on an outcome. It is deterministic -- ties break on the unit key, and
    nothing is iterated in insertion order (invariant 7). And it is resumable:
    files already ingested (``done``) count as evidence held, so re-planning
    after an interruption continues the same allocation instead of restarting
    it.
    """
    if max_files < 0:
        raise ValueError(f"max_files must not be negative, got {max_files}")
    if half_life_days <= 0 or lookback_days <= 0:
        raise ValueError("half_life_days and lookback_days must be positive")
    if equity < 1.0:
        raise ValueError(f"equity must be at least 1 (log utility), got {equity}")
    if floor_days <= 0 or floor_min_rescued < 1:
        raise ValueError("floor_days must be positive and floor_min_rescued at least 1")

    def utility(total: float) -> float:
        # Isoelastic in (1 + evidence): log at equity 1, more concave above it.
        if equity == 1.0:
            return math.log1p(total)
        return float(math.pow(1.0 + total, 1.0 - equity) - 1.0) / (1.0 - equity)

    scenarios = sorted(cutoffs)
    everything = [file for month in sorted(listings) for file in listings[month]]
    by_key = {unit_key(file): file for file in everything}
    held = sorted(set(done))

    def worths(file: CrawlFile) -> list[float]:
        return [
            worth(file, cutoff, half_life_days=half_life_days, lookback_days=lookback_days)
            for cutoff in scenarios
        ]

    evidence = [0.0] * len(scenarios)
    for key in held:
        file = by_key.get(key)
        if file is None:
            continue  # ingested from a month whose listing was not supplied
        for position, value in enumerate(worths(file)):
            evidence[position] += value

    pool = {
        unit_key(file): worths(file)
        for file in _candidates(everything, scenarios)
        if unit_key(file) not in set(held)
    }
    pool = {key: values for key, values in sorted(pool.items()) if any(values)}

    plan: list[str] = []

    # -- phase 1: the floor ------------------------------------------------
    files_in_pool = {key: by_key[key] for key in sorted(pool)}

    def gap_days(file: CrawlFile, cutoff: datetime) -> float:
        return (cutoff - file.crawled_until).total_seconds() / 86_400.0

    def has_floor(cutoff: datetime, keys: Iterable[str]) -> bool:
        return any(
            0.0 <= gap_days(by_key[key], cutoff) <= floor_days for key in keys if key in by_key
        )

    lacking = [cutoff for cutoff in scenarios if not has_floor(cutoff, held)]
    while lacking and files_in_pool and len(plan) < max_files:
        best_key, best_score = "", (0, 0.0)
        for key, file in sorted(files_in_pool.items()):
            rescued = sum(0.0 <= gap_days(file, cutoff) <= floor_days for cutoff in lacking)
            # Most rescued; between equals, the later file -- it sits closer to
            # the cutoffs it serves. Strict `>` keeps the first (sorted) key on
            # a full tie.
            score = (rescued, file.crawled_until.timestamp())
            if score > best_score:
                best_key, best_score = key, score
        if best_score[0] < floor_min_rescued:
            break
        chosen = files_in_pool.pop(best_key)
        lacking = [c for c in lacking if not 0.0 <= gap_days(chosen, c) <= floor_days]
        for position, value in enumerate(pool.pop(best_key)):
            evidence[position] += value
        plan.append(best_key)

    # -- phase 2: closeness ------------------------------------------------
    while pool and len(plan) < max_files:
        best_key, best_gain = "", 0.0
        for key, values in sorted(pool.items()):
            gain = math.fsum(
                utility(evidence[i] + value) - utility(evidence[i])
                for i, value in enumerate(values)
                if value
            )
            if gain > best_gain:  # strict: the first key wins a tie, and keys are sorted
                best_key, best_gain = key, gain
        if not best_key:
            break
        for position, value in enumerate(pool.pop(best_key)):
            evidence[position] += value
        plan.append(best_key)
    return plan

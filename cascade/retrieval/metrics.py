"""Pure measurement functions for the retrieval bench (spec §4.2).

No clock, no RNG from global state, no I/O -- the whole module is a function
of its arguments, which is what makes the percentile and recall definitions
property-testable rather than merely plausible.

Two definitions are pinned here because the acceptance criteria are stated to
three significant figures and an unstated definition is not reproducible:

* **Percentiles** use linear interpolation between the two closest ranks on
  the sorted sample -- the same definition as ``numpy.percentile`` with its
  default ``method='linear'``. Nearest-rank would report a p95 up to one
  sample's width lower on a 10,000-point set, which is enough to move a 15 ms
  verdict.
* **recall@k** is ``|approx_k ∩ exact_k| / |exact_k|`` per query, averaged over
  queries that returned anything. Queries whose cutoff admits no evidence at
  all have no ground truth to recall and are counted separately rather than
  scored as 1.0 -- scoring them as perfect would let an empty corpus report
  perfect recall, which is precisely backwards.

**Outcome-blind relevance (M14).** Recall says how well the index approximates
exact vector search; it cannot say whether vector search was finding the right
evidence. Whether a change to retrieval *helps* has to be decided without a
forecast -- deciding it by Brier would be tuning retrieval against outcomes --
so three properties of a retrieved set are measured from the set alone:

* the share of chunks that **name one of the scenario's parties**,
* the **median age** of the chunks at the cutoff, in days, and
* the number of **distinct documents** among them,

with the set's **mean embedding distance** beside them as the cost side: every
one of the three can be bought by retrieving things further from the query,
and a report that showed only the benefits would not be a measurement.

These are properties the hybrid path was *built* to move -- the keyword pool
matches names, fusion ranks recency, the diversity pass spreads documents. So
they verify the mechanisms work on the real corpus and at what cost in
distance; they are not independent evidence that forecasts improve.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from cascade.retrieval.schema import LatencySummary, SearchResult

__all__ = [
    "RecallSummary",
    "RelevanceScore",
    "generic_names",
    "histogram",
    "informative",
    "mentions",
    "mentions_any",
    "percentile",
    "recall_at_k",
    "score_retrieval",
    "summarise_latency",
]

# Bucket edges in milliseconds. Dense below the 15 ms budget because that is
# where the verdict is decided, then coarse -- a run whose tail is past 100 ms
# has failed and the exact shape of the failure is not what the reader needs.
_HISTOGRAM_EDGES: tuple[float, ...] = (
    0.0,
    1.0,
    2.0,
    3.0,
    5.0,
    7.5,
    10.0,
    12.5,
    15.0,
    20.0,
    30.0,
    50.0,
    100.0,
    float("inf"),
)


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q``-th percentile of ``values`` (``q`` in [0, 100]).

    Preserves the definition the acceptance criteria are read against: linear
    interpolation between closest ranks, identical to ``numpy.percentile``'s
    default. Implemented here rather than delegated so the pure core stays
    free of an array dependency and the definition is visible at the point it
    is relied on.
    """
    if not values:
        raise ValueError("percentile of an empty sample is undefined")
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"percentile q must be in [0, 100], got {q}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * (q / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    low, high = ordered[lower], ordered[upper]
    # `low + (high - low) * weight`, not `low * (1 - weight) + high * weight`.
    # The two are equivalent in exact arithmetic and not in floating point:
    # when low == high the second form computes `a*0.11 + a*0.89`, which can
    # land one ULP below `a`. A property test caught it as p99 < p95 on a
    # sample whose top two values were equal -- a monotonicity violation in
    # the reported percentiles, which is exactly the kind of defect that would
    # be dismissed as noise if it ever surfaced in a bench report.
    # This is the same form numpy uses.
    return float(low + (high - low) * weight)


def histogram(
    values: Sequence[float], edges: Sequence[float] = _HISTOGRAM_EDGES
) -> tuple[tuple[float, float, int], ...]:
    """Bucket ``values`` into ``[lower, upper)`` bins.

    Half-open upwards so a value exactly on an edge lands in one bucket only
    and the counts sum to ``len(values)``; the final bucket is unbounded so
    nothing is silently dropped off the top.
    """
    if len(edges) < 2:
        raise ValueError("a histogram needs at least two edges")
    counts = [0] * (len(edges) - 1)
    for value in values:
        for index in range(len(counts)):
            if edges[index] <= value < edges[index + 1]:
                counts[index] += 1
                break
    return tuple(
        (float(edges[index]), float(edges[index + 1]), counts[index])
        for index in range(len(counts))
    )


def summarise_latency(samples: Sequence[float]) -> LatencySummary:
    """Percentiles and a histogram over measured latencies, in milliseconds."""
    if not samples:
        return LatencySummary(
            n=0,
            p50=0.0,
            p95=0.0,
            p99=0.0,
            minimum=0.0,
            maximum=0.0,
            histogram=histogram([]),
        )
    return LatencySummary(
        n=len(samples),
        p50=percentile(samples, 50.0),
        p95=percentile(samples, 95.0),
        p99=percentile(samples, 99.0),
        minimum=float(min(samples)),
        maximum=float(max(samples)),
        histogram=histogram(samples),
    )


@dataclass(frozen=True)
class RecallSummary:
    """recall@k over a sample of queries.

    ``empty_ground_truth`` is reported rather than folded into the mean: those
    queries had no admissible evidence at their cutoff, which is a fact about
    corpus coverage and belongs in the report next to the recall number, not
    inside it.
    """

    k: int
    scored: int
    empty_ground_truth: int
    mean: float
    minimum: float
    perfect: int

    @property
    def measured(self) -> bool:
        """False when nothing could be scored, so a caller cannot read 0.0 as a result."""
        return self.scored > 0


def recall_at_k(pairs: Iterable[tuple[Sequence[str], Sequence[str]]], *, k: int) -> RecallSummary:
    """Mean recall@k of approximate results against exact ground truth.

    ``pairs`` yields ``(approximate_ids, exact_ids)`` per query, each already
    ordered nearest-first. Both are truncated to ``k`` before comparison: an
    approximate search asked for more than it is scored on would be credited
    for hits outside the window the criterion is about.

    Compared as sets. Rank order within the top k is not part of the recall@20
    criterion, and requiring it would be a stricter, different measurement
    reported under the criterion's name.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    scores: list[float] = []
    empty = 0
    perfect = 0
    for approximate, exact in pairs:
        truth = set(exact[:k])
        if not truth:
            empty += 1
            continue
        found = set(approximate[:k]) & truth
        score = len(found) / len(truth)
        scores.append(score)
        if score == 1.0:
            perfect += 1
    return RecallSummary(
        k=k,
        scored=len(scores),
        empty_ground_truth=empty,
        mean=sum(scores) / len(scores) if scores else 0.0,
        minimum=min(scores) if scores else 0.0,
        perfect=perfect,
    )


# ---------------------------------------------------------------------------
# Outcome-blind relevance (M14)
# ---------------------------------------------------------------------------

_SECONDS_PER_DAY = 86_400.0
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'"})


def _normalise(text: str) -> str:
    """Casefold, straighten apostrophes, collapse whitespace.

    Applied to the name and the body alike, so a name split across a line
    break, or written with the other apostrophe, is the same name.
    """
    return " ".join(text.translate(_APOSTROPHES).casefold().split())


@lru_cache(maxsize=4096)
def _name_pattern(normalised_name: str) -> re.Pattern[str]:
    # `(?<!\w)` / `(?!\w)` rather than `\b`: `\b` needs a word character on one
    # side, so it never matches at the edge of a name that begins or ends with
    # punctuation ("AT&T", "Yes'"), and such a name would silently match nothing.
    return re.compile(rf"(?<!\w){re.escape(normalised_name)}(?!\w)")


def mentions(body: str, name: str, *, normalised_body: str | None = None) -> bool:
    """Whether ``body`` names ``name`` as a whole word or phrase.

    Case-insensitive, and bounded on both sides by a non-word character, so
    "Apple" is found in "Apple's appeal" and not in "pineapple". A blank name
    mentions nothing: matching it would make every chunk a hit.

    ``normalised_body`` lets a caller that tests many names against one body
    pay for the normalisation once.
    """
    wanted = _normalise(name)
    if not wanted:
        return False
    haystack = normalised_body if normalised_body is not None else _normalise(body)
    # A substring test first: it is a C-speed scan that rejects nearly every
    # (body, name) pair, and the regex only has to adjudicate the boundaries.
    if wanted not in haystack:
        return False
    return _name_pattern(wanted).search(haystack) is not None


def mentions_any(body: str, names: Sequence[str]) -> bool:
    """Whether ``body`` names at least one of ``names``."""
    haystack = _normalise(body)
    return any(mentions(body, name, normalised_body=haystack) for name in names)


@dataclass(frozen=True)
class RelevanceScore:
    """The outcome-blind properties of one retrieved set.

    ``None`` means *not measurable*, never zero. A scenario with no usable
    party names has no mention rate -- scoring it 0.0 would read as "retrieved
    nothing relevant", and 1.0 as the opposite, when the truth is that nothing
    was looked for. An empty result has no median age for the same reason.
    Callers average over the measurable and report how many were not.
    """

    returned: int
    party_mention_rate: float | None
    median_age_days: float | None
    distinct_documents: int
    mean_distance: float | None


def score_retrieval(result: SearchResult, party_names: Sequence[str]) -> RelevanceScore:
    """Score one retrieved set against the names it should be about.

    Reads nothing but the result and the names: no label, no resolution text,
    no forecast. That is what lets retrieval be compared and chosen without
    the choice being fitted to the outcomes the study will be scored on.

    Age is taken against ``result.as_of`` -- the cutoff the set was retrieved
    under travels with it -- so a score cannot be computed against a cutoff
    other than the one that produced the chunks.
    """
    chunks = result.chunks
    names = [name for name in party_names if name.strip()]

    mention_rate: float | None = None
    if names and chunks:
        mention_rate = sum(1 for chunk in chunks if mentions_any(chunk.body, names)) / len(chunks)

    median_age: float | None = None
    mean_distance: float | None = None
    if chunks:
        ages = [
            (result.as_of - chunk.published_at).total_seconds() / _SECONDS_PER_DAY
            for chunk in chunks
        ]
        median_age = percentile(ages, 50.0)
        # Sorted before summing: float addition is not associative, and this
        # number is diffed between runs.
        mean_distance = sum(sorted(chunk.distance for chunk in chunks)) / len(chunks)

    return RelevanceScore(
        returned=len(chunks),
        party_mention_rate=mention_rate,
        median_age_days=median_age,
        distinct_documents=len({chunk.document_id for chunk in chunks}),
        mean_distance=mean_distance,
    )


def generic_names(
    bodies_by_scenario: Mapping[str, Sequence[str]],
    names_by_scenario: Mapping[str, Sequence[str]],
    *,
    max_background_rate: float,
) -> frozenset[str]:
    """Registry names too common to identify a party, casefolded.

    The registry's ``party_names`` are capitalised runs from the question *and
    its resolution boilerplate*, so beside "Baltimore Ravens" they hold "PM
    ET", "Other", "However" and "Associated Press". A chunk "mentions a party"
    if it contains the word "other", which pins the mention rate near 1.0 for
    both arms and hides the difference being measured.

    A name's **background rate** is the share of chunks retrieved for scenarios
    that do *not* list it which mention it anyway. A real party is rare outside
    its own scenarios; boilerplate is everywhere. The rule is outcome-blind (it
    reads bodies and names), arm-blind when the caller pools both arms' chunks,
    and self-calibrating -- it needs no hand-kept stoplist to go stale when the
    registry changes. It also bounds the damage of what it lets through: a name
    that survives inflates a mention rate by at most ``max_background_rate``.

    A name every scenario lists has no background to measure and is treated as
    generic: a name that cannot tell one scenario from another identifies none.
    """
    if not 0.0 < max_background_rate <= 1.0:
        raise ValueError(f"max_background_rate must lie in (0, 1], got {max_background_rate}")

    listed_by: dict[str, set[str]] = {}
    for scenario_id in sorted(names_by_scenario):
        for name in names_by_scenario[scenario_id]:
            key = _normalise(name)
            if key:
                listed_by.setdefault(key, set()).add(scenario_id)

    normalised = {
        scenario_id: [_normalise(body) for body in bodies_by_scenario[scenario_id]]
        for scenario_id in sorted(bodies_by_scenario)
    }

    generic: set[str] = set()
    for key in sorted(listed_by):
        total = 0
        hits = 0
        for scenario_id in sorted(normalised):
            if scenario_id in listed_by[key]:
                continue
            for haystack in normalised[scenario_id]:
                total += 1
                if mentions("", key, normalised_body=haystack):
                    hits += 1
        if total == 0 or hits / total > max_background_rate:
            generic.add(key)
    return frozenset(generic)


def informative(names: Sequence[str], generic: frozenset[str]) -> tuple[str, ...]:
    """``names`` without the generic ones, order kept."""
    return tuple(name for name in names if _normalise(name) and _normalise(name) not in generic)

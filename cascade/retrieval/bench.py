"""The Chronofence bench: latency and recall (spec §4.2).

Reports p50/p95/p99 **and** recall@20 against exhaustive search. Both, always,
in the same report. A latency number without a recall number says only that
the index returned something quickly, and the cheapest way to return something
quickly is to return the wrong thing -- `ef_search = 1` would halve the p95 and
quietly cost most of the recall the study depends on.

Recall is measured on a sample (`retrieval.bench_recall_sample`, 5%) because
the oracle is exhaustive: on this corpus one exact query costs roughly two
orders of magnitude more than one indexed query, so scoring all 10,000 would
turn a two-minute bench into a multi-hour one for a number that is already
tight at 500 samples.

Query embedding is excluded from the measured interval. The 15 ms budget in
§4.2 is a budget for the *retrieval*, and an agent's query vector at M5 comes
from the same embedder amortised across a batch; folding a cold single-text
encode into every sample would measure the model, not the index.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from cascade.config import Settings
from cascade.corpus.embed import Embedder
from cascade.eval.schema import BootstrapInterval
from cascade.eval.stats import bootstrap_differences, bootstrap_seed
from cascade.retrieval.metrics import (
    RecallSummary,
    RelevanceScore,
    generic_names,
    informative,
    recall_at_k,
    score_retrieval,
    summarise_latency,
)
from cascade.retrieval.queries import (
    BenchQuery,
    GraphLike,
    QueryKind,
    RelevanceQuery,
    build_queries,
    relevance_queries,
)
from cascade.retrieval.schema import LatencySummary, PartitionIndex, SearchResult
from cascade.retrieval.search import Chronofence

__all__ = [
    "MIN_RECALL_SAMPLE",
    "RELEVANCE_METRICS",
    "ArmPair",
    "BenchResult",
    "HybridNotReady",
    "KindReport",
    "MetricComparison",
    "RelevanceReport",
    "hybrid_readiness",
    "run_bench",
    "run_relevance",
    "summarise_relevance",
]

# Below this many scored queries, recall@k is too noisy to decide a criterion
# stated to two decimal places. At `--queries 100` the 5% sample is 5 queries,
# where a single imperfect result moves the mean by 0.04 -- twice the margin
# the criterion is judged on. The bench still refuses to pass in that case, but
# it says the sample was too small rather than reporting a verdict it cannot
# support.
MIN_RECALL_SAMPLE = 30


@dataclass(frozen=True)
class BenchResult:
    """Everything the M3 acceptance criteria are read off.

    `empty_results` is carried alongside the percentiles because a query that
    matched nothing is fast, and a bench that met its p95 by retrieving
    nothing would otherwise look like a pass.
    """

    queries: int
    k: int
    latency: LatencySummary
    recall: RecallSummary
    empty_results: int
    distinct_cutoffs: int
    earliest_cutoff: str
    latest_cutoff: str
    ef_search: int
    """The `hnsw.ef_search` pinned into the deployed function, not the
    configured one: a bench that read config would be comparing config with
    itself."""
    elapsed_s: float

    def meets(self, settings: Settings) -> tuple[bool, tuple[str, ...]]:
        """Verdict against the configured targets, with the reasons it failed.

        Returns measured failures, never a tolerance. If p95 lands at 15.4 ms
        against a 15 ms budget the answer is that it failed at 15.4 ms.
        """
        target = settings.retrieval
        failures: list[str] = []
        if self.latency.n == 0:
            failures.append("no queries were executed")
        if self.latency.p95 >= target.target_p95_ms:
            failures.append(
                f"p95 {self.latency.p95:.2f} ms is not below the "
                f"{target.target_p95_ms:.0f} ms budget"
            )
        if not self.recall.measured:
            failures.append(
                f"recall@{self.recall.k} could not be measured: no sampled query "
                "had any admissible evidence at its cutoff"
            )
        elif self.recall.scored < MIN_RECALL_SAMPLE:
            failures.append(
                f"recall@{self.recall.k} measured {self.recall.mean:.4f} on only "
                f"{self.recall.scored} sampled queries, too few to decide a criterion "
                f"stated to two decimals -- run the configured "
                f"{settings.retrieval.bench_queries:,}-query bench rather than a subset"
            )
        elif self.recall.mean <= target.target_recall_at_k:
            failures.append(
                f"recall@{self.recall.k} {self.recall.mean:.4f} is not above "
                f"{target.target_recall_at_k:.2f}"
            )
        return (not failures, tuple(failures))


def _sample_indices(total: int, fraction: float) -> frozenset[int]:
    """Evenly spaced sample positions.

    Evenly spaced rather than random: the query set is built round-robin over
    scenarios ordered by id, so a stride samples every scenario roughly
    equally, while a random draw of 5% would leave whole cutoffs unmeasured
    and make the recall number depend on the seed.
    """
    if total <= 0:
        return frozenset()
    wanted = max(1, round(total * fraction))
    if wanted >= total:
        return frozenset(range(total))
    stride = total / wanted
    return frozenset(min(total - 1, round(index * stride)) for index in range(wanted))


def run_bench(
    settings: Settings,
    *,
    queries: Sequence[BenchQuery] | None = None,
    count: int | None = None,
    progress: object = None,
) -> BenchResult:
    """Run the bench and return measured values.

    ``progress`` is any object with an ``advance(int)`` method, or None. Typed
    loosely on purpose: the pure measurement path must not acquire a Rich
    dependency to report itself.
    """
    from cascade.ledger.store import load_scenarios

    retrieval = settings.retrieval
    started = time.monotonic()

    if queries is None:
        scenarios = load_scenarios(settings, role="admin")
        queries = build_queries(
            scenarios,
            count=count if count is not None else retrieval.bench_queries,
            salt=settings.study.salt,
        )
    if not queries:
        raise ValueError("bench requires at least one query")

    embedder = Embedder(
        model_name=settings.models.embedding,
        batch_size=settings.corpus.embed_batch_size,
    )
    # Embedded up front, in one batch, so the measured interval contains only
    # the database round trip. Also amortises model load across the whole run.
    vectors = embedder.encode([query.text for query in queries])

    sampled = _sample_indices(len(queries), retrieval.bench_recall_sample)
    k = retrieval.bench_recall_k

    latencies: list[float] = []
    empty = 0
    pairs: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    # `eval` rather than `sim`: the exact oracle is granted only to eval, and
    # running both arms on one connection keeps the comparison honest about
    # cache state. The indexed arm is identical under either role -- the same
    # SECURITY DEFINER function with the same pinned ef_search.
    with Chronofence(settings, role="eval") as fence:
        ef_search = fence.ef_search()
        for index, (query, vector) in enumerate(zip(queries, vectors, strict=True)):
            result = fence.search(vector, as_of=query.as_of, k=k)
            latencies.append(result.elapsed_ms)
            if not result.chunks:
                empty += 1

            if index in sampled:
                truth = fence.search_exact(vector, as_of=query.as_of, k=k)
                pairs.append((result.chunk_ids, truth.chunk_ids))

            if progress is not None:
                advance = getattr(progress, "advance", None)
                if callable(advance):
                    advance(1)

    cutoffs = sorted({query.as_of for query in queries})
    return BenchResult(
        queries=len(queries),
        k=k,
        latency=summarise_latency(latencies),
        recall=recall_at_k(pairs, k=k),
        empty_results=empty,
        distinct_cutoffs=len(cutoffs),
        earliest_cutoff=cutoffs[0].isoformat(),
        latest_cutoff=cutoffs[-1].isoformat(),
        ef_search=ef_search,
        elapsed_s=time.monotonic() - started,
    )


# ---------------------------------------------------------------------------
# bench --relevance: vector against hybrid, on the study's own queries (M14)
#
# This is how `retrieval.mode` gets decided, so it has to be decidable without
# a forecast. It reads scenarios and compiled graphs -- never a label -- and
# runs as `cascade_sim`, which has no grant on `scenario_labels` (invariant 2):
# the comparison cannot be fitted to outcomes because it cannot see one.
# ---------------------------------------------------------------------------

# (attribute of RelevanceScore, label, whether it reads the party names).
RELEVANCE_METRICS: tuple[tuple[str, str, bool], ...] = (
    ("party_mention_rate", "chunks naming a party", True),
    ("median_age_days", "median age at cutoff (days)", False),
    ("distinct_documents", "distinct documents", False),
    ("mean_distance", "mean embedding distance (cost)", False),
)


class HybridNotReady(RuntimeError):
    """The hybrid path cannot be measured on this database yet.

    Raised before any query runs. Without the full-text index the keyword
    predicate parses every pre-cutoff body per query, so the bench would not
    fail -- it would run for days and then report a latency that describes a
    missing index rather than the design.
    """

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


def hybrid_readiness(*, deployed: bool, partitions: Sequence[PartitionIndex]) -> tuple[str, ...]:
    """Why the hybrid path is not usable, or ``()`` when it is. Pure."""
    reasons: list[str] = []
    if not deployed:
        reasons.append(
            "chronofence_search_hybrid is absent: migration 018 has not been applied "
            "(run `cascade db migrate`)"
        )
    missing = sorted(
        item.partition for item in partitions if item.rows > 0 and item.fts_index_name is None
    )
    if missing:
        reasons.append(
            f"{len(missing)} non-empty partition(s) have no full-text index: "
            + ", ".join(missing[:6])
            + ("..." if len(missing) > 6 else "")
            + " -- run `cascade retrieval index --fts`"
        )
    return tuple(reasons)


@dataclass(frozen=True)
class ArmPair:
    """One query answered by both arms."""

    query: RelevanceQuery
    vector: SearchResult
    hybrid: SearchResult


@dataclass(frozen=True)
class MetricComparison:
    """One metric, both arms, paired over scenarios.

    ``interval`` is on ``hybrid - vector``. ``unmeasurable`` counts scenarios
    left out of the pairing because the metric had no value in one arm or the
    other -- no usable party name, or nothing retrieved -- and is reported
    beside the means rather than folded into them.
    """

    metric: str
    label: str
    names: str
    n_paired: int
    unmeasurable: int
    vector_mean: float | None
    hybrid_mean: float | None
    interval: BootstrapInterval | None


@dataclass(frozen=True)
class KindReport:
    """Everything measured for one kind of query (compiler, agent, baseline)."""

    kind: QueryKind
    k: int
    scenarios: int
    queries: int
    comparisons: tuple[MetricComparison, ...]
    mean_overlap: float
    """Mean Jaccard overlap of the two arms' chunk ids: how different the
    evidence actually is. Near 1.0 means the switch would change little."""
    queries_without_terms: int
    """Hybrid queries that found no entity term, and so ran as the vector pool
    re-ranked by recency and diversity."""
    vector_latency: LatencySummary
    hybrid_latency: LatencySummary


@dataclass(frozen=True)
class RelevanceReport:
    kinds: tuple[KindReport, ...]
    scenarios: int
    graphs: int
    distinct_names: int
    generic: tuple[str, ...]
    scenarios_without_informative_names: int
    bootstrap_b: int
    elapsed_s: float


def _mean(values: Sequence[float]) -> float:
    # Sorted before summing, as the Brier is (M7): this is a reported number,
    # and float addition is not associative.
    return sum(sorted(values)) / len(values)


def _per_scenario(
    pairs: Sequence[ArmPair],
    names_by_scenario: dict[str, tuple[str, ...]],
    metric: str,
) -> dict[str, tuple[float | None, float | None]]:
    """``scenario -> (vector, hybrid)``, each the mean over that scenario's queries.

    The scenario is the unit because it is the independent one: an ``agent``
    reading has a dozen queries per scenario that share a question, and
    resampling them as though independent would shrink every interval.
    """
    collected: dict[str, tuple[list[float], list[float]]] = {}
    for pair in pairs:
        names = names_by_scenario.get(pair.query.scenario_id, ())
        scores: tuple[RelevanceScore, RelevanceScore] = (
            score_retrieval(pair.vector, names),
            score_retrieval(pair.hybrid, names),
        )
        bucket = collected.setdefault(pair.query.scenario_id, ([], []))
        for arm, score in enumerate(scores):
            value = getattr(score, metric)
            if value is not None:
                bucket[arm].append(float(value))
    return {
        scenario_id: (
            _mean(collected[scenario_id][0]) if collected[scenario_id][0] else None,
            _mean(collected[scenario_id][1]) if collected[scenario_id][1] else None,
        )
        for scenario_id in sorted(collected)
    }


def _compare(
    pairs: Sequence[ArmPair],
    names_by_scenario: dict[str, tuple[str, ...]],
    *,
    metric: str,
    label: str,
    names: str,
    seed_parts: tuple[str, ...],
    salt: str,
    b_resamples: int,
) -> MetricComparison:
    per_scenario = _per_scenario(pairs, names_by_scenario, metric)
    paired = [
        (vector, hybrid)
        for scenario_id in sorted(per_scenario)
        for vector, hybrid in (per_scenario[scenario_id],)
        if vector is not None and hybrid is not None
    ]
    unmeasurable = len(per_scenario) - len(paired)
    if not paired:
        return MetricComparison(
            metric=metric,
            label=label,
            names=names,
            n_paired=0,
            unmeasurable=unmeasurable,
            vector_mean=None,
            hybrid_mean=None,
            interval=None,
        )
    differences = [hybrid - vector for vector, hybrid in paired]
    interval = bootstrap_differences(
        differences,
        point=_mean(differences),
        seed=bootstrap_seed(salt, "retrieval-relevance", *seed_parts, metric, names),
        b_resamples=b_resamples,
    )
    return MetricComparison(
        metric=metric,
        label=label,
        names=names,
        n_paired=len(paired),
        unmeasurable=unmeasurable,
        vector_mean=_mean([vector for vector, _ in paired]),
        hybrid_mean=_mean([hybrid for _, hybrid in paired]),
        interval=interval,
    )


def _overlap(left: SearchResult, right: SearchResult) -> float:
    a, b = set(left.chunk_ids), set(right.chunk_ids)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def summarise_relevance(
    pairs: Sequence[ArmPair],
    *,
    party_names: dict[str, tuple[str, ...]],
    salt: str,
    b_resamples: int,
    max_background_rate: float,
    graphs: int,
    elapsed_s: float,
) -> RelevanceReport:
    """Turn paired results into the report. No I/O; seeded, so reproducible.

    The mention rate is reported twice -- over every registry name, and over
    the informative ones (:func:`~cascade.retrieval.metrics.generic_names`) --
    because the registry's names include resolution boilerplate. The first is
    the literal reading; the second is the one that can show a difference.
    Printing both keeps the filter from being a place a result could hide.

    The generic-name screen pools **both arms'** chunks, so which names survive
    cannot depend on which arm is being flattered.
    """
    bodies: dict[str, dict[str, str]] = {}
    for pair in pairs:
        pool = bodies.setdefault(pair.query.scenario_id, {})
        for chunk in (*pair.vector.chunks, *pair.hybrid.chunks):
            pool[chunk.chunk_id] = chunk.body
    generic = generic_names(
        {sid: [bodies[sid][cid] for cid in sorted(bodies[sid])] for sid in sorted(bodies)},
        {sid: party_names[sid] for sid in sorted(party_names)},
        max_background_rate=max_background_rate,
    )
    informative_names = {sid: informative(party_names[sid], generic) for sid in sorted(party_names)}

    kinds: list[KindReport] = []
    for kind in ("compiler", "agent", "baseline"):
        of_kind = [pair for pair in pairs if pair.query.kind == kind]
        if not of_kind:
            continue
        comparisons: list[MetricComparison] = []
        for metric, label, reads_names in RELEVANCE_METRICS:
            readings = (
                (("all registry names", party_names), ("informative names", informative_names))
                if reads_names
                else (("-", party_names),)
            )
            for reading, names_by_scenario in readings:
                comparisons.append(
                    _compare(
                        of_kind,
                        names_by_scenario,
                        metric=metric,
                        label=label,
                        names=reading,
                        seed_parts=(kind,),
                        salt=salt,
                        b_resamples=b_resamples,
                    )
                )
        kinds.append(
            KindReport(
                kind=kind,
                k=of_kind[0].query.k,
                scenarios=len({pair.query.scenario_id for pair in of_kind}),
                queries=len(of_kind),
                comparisons=tuple(comparisons),
                mean_overlap=_mean([_overlap(pair.vector, pair.hybrid) for pair in of_kind]),
                queries_without_terms=sum(1 for pair in of_kind if not pair.hybrid.terms),
                vector_latency=summarise_latency([pair.vector.elapsed_ms for pair in of_kind]),
                hybrid_latency=summarise_latency([pair.hybrid.elapsed_ms for pair in of_kind]),
            )
        )

    return RelevanceReport(
        kinds=tuple(kinds),
        scenarios=len({pair.query.scenario_id for pair in pairs}),
        graphs=graphs,
        distinct_names=len(
            {name.casefold() for sid in sorted(party_names) for name in party_names[sid]}
        ),
        generic=tuple(sorted(generic)),
        scenarios_without_informative_names=sum(
            1 for sid in sorted(informative_names) if not informative_names[sid]
        ),
        bootstrap_b=b_resamples,
        elapsed_s=elapsed_s,
    )


def run_relevance(
    settings: Settings,
    *,
    limit: int | None = None,
    encode: Callable[[list[str]], Any] | None = None,
    progress: object = None,
) -> RelevanceReport:
    """Run both retrieval arms over the study's real queries and compare them.

    Preserves invariant 2 by construction: every read here is made as
    ``cascade_sim`` -- scenarios, graphs and both search functions -- and that
    role has no grant on ``scenario_labels``. The one exception is the
    readiness check, which reads the *catalogue* (which partitions carry which
    index) through the admin-only partition view, exactly as `retrieval verify`
    does; it touches no table of the study.

    Raises :class:`HybridNotReady` before the first query when migration 018 or
    a full-text index is missing.

    Both arms run on one connection, back to back per query, so they see the
    same corpus and the same cache state. ``limit`` takes the first N
    scenarios by id -- a smoke run, deterministic, and labelled as partial by
    the scenario count in the report.
    """
    from cascade.decompose.store import load_graphs
    from cascade.ledger.store import load_scenarios
    from cascade.retrieval.index import measure

    started = time.monotonic()
    retrieval = settings.retrieval

    with Chronofence(settings, role="sim") as probe:
        deployed = probe.hybrid_deployed()
    reasons = hybrid_readiness(deployed=deployed, partitions=measure(settings) if deployed else ())
    if reasons:
        raise HybridNotReady(reasons)

    scenarios = sorted(load_scenarios(settings, role="sim"), key=lambda item: item.scenario_id)
    if limit is not None:
        scenarios = scenarios[: max(1, limit)]
    if not scenarios:
        raise ValueError("scenario registry is empty; run `cascade ledger build`")
    wanted = {scenario.scenario_id for scenario in scenarios}
    graphs: list[GraphLike] = [
        graph for graph in load_graphs(settings, role="sim") if graph.scenario_id in wanted
    ]

    queries = relevance_queries(
        scenarios, graphs, k_agent=retrieval.k_agent, k_compiler=retrieval.k_compiler
    )

    if encode is None:
        embedder = Embedder(
            model_name=settings.models.embedding, batch_size=settings.corpus.embed_batch_size
        )
        encode = embedder.encode
    vectors = encode([item.query.text for item in queries])

    pairs: list[ArmPair] = []
    with Chronofence(settings, role="sim") as fence:
        for item, vector in zip(queries, vectors, strict=True):
            pairs.append(
                ArmPair(
                    query=item,
                    vector=fence.search(vector, as_of=item.as_of, k=item.k),
                    hybrid=fence.search_hybrid(
                        vector,
                        text=item.query.keyword_text,
                        entities=item.query.entities,
                        as_of=item.as_of,
                        k=item.k,
                    ),
                )
            )
            if progress is not None:
                advance = getattr(progress, "advance", None)
                if callable(advance):
                    advance(1)

    return summarise_relevance(
        pairs,
        party_names={scenario.scenario_id: scenario.party_names for scenario in scenarios},
        salt=settings.study.salt,
        b_resamples=settings.ensemble.bootstrap_b,
        max_background_rate=retrieval.relevance_generic_name_rate,
        graphs=len(graphs),
        elapsed_s=time.monotonic() - started,
    )

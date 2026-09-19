"""Chronofence: time-locked retrieval (M3, spec §4).

``as_of`` is a required parameter everywhere in this package -- no default in
Python, no default in SQL (invariant 1). The time filter is structural: the
planner prunes post-cutoff partitions before touching a vector, so the lock
does not depend on a caller remembering a predicate.

The public surface is deliberately small. Everything that reads the corpus
goes through :class:`Chronofence`, which binds one database role for its
lifetime; opened as ``sim`` -- what the simulation actually runs as -- the
exhaustive oracle is not reachable, because ``cascade_sim`` is not granted it.

Two read paths exist and ``retrieval.mode`` chooses between them (M14).
``vector`` is ``chronofence_search``: nearest chunks by embedding distance.
``hybrid`` is ``chronofence_search_hybrid``: that same vector pool beside a
full-text pool of chunks that *name* the parties, fused by reciprocal rank with
a recency ranking and passed through near-duplicate suppression. Both are
time-locked inside their SQL function; the fusion is pure Python that can only
reorder what it was handed.
"""

from __future__ import annotations

from cascade.retrieval.bench import BenchResult, RelevanceReport, run_bench, run_relevance
from cascade.retrieval.fusion import FusionParams, fuse, select
from cascade.retrieval.index import (
    apply_fts_plans,
    apply_plans,
    drop_legacy_ivfflat,
    measure,
    plan_all,
    plan_fts,
)
from cascade.retrieval.keywords import entity_terms
from cascade.retrieval.metrics import (
    RecallSummary,
    histogram,
    percentile,
    recall_at_k,
    summarise_latency,
)
from cascade.retrieval.queries import BenchQuery, build_queries
from cascade.retrieval.schema import (
    HybridCandidate,
    IndexPlan,
    IndexReport,
    LatencySummary,
    PartitionIndex,
    RetrievedChunk,
    SearchResult,
)
from cascade.retrieval.search import Chronofence, TimeLockViolation

__all__ = [
    "BenchQuery",
    "BenchResult",
    "Chronofence",
    "FusionParams",
    "HybridCandidate",
    "IndexPlan",
    "IndexReport",
    "LatencySummary",
    "PartitionIndex",
    "RecallSummary",
    "RelevanceReport",
    "RetrievedChunk",
    "SearchResult",
    "TimeLockViolation",
    "apply_fts_plans",
    "apply_plans",
    "build_queries",
    "drop_legacy_ivfflat",
    "entity_terms",
    "fuse",
    "histogram",
    "measure",
    "percentile",
    "plan_all",
    "plan_fts",
    "recall_at_k",
    "run_bench",
    "run_relevance",
    "select",
    "summarise_latency",
]

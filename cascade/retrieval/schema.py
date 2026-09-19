"""Boundary types for Chronofence (spec §4.2).

Every type here carries ``as_of`` or is derived from something that did. That
is deliberate: a retrieval result with no record of the cutoff it was taken
under cannot be audited later, and M8 has to be able to walk from an outcome
back to the evidence that produced it and prove the evidence was admissible.

Pydantic v2 at the boundary, per the engineering standards -- a raw dict
crossing out of this package is a bug.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "FtsIndexPlan",
    "FtsIndexReport",
    "HybridCandidate",
    "IndexAction",
    "IndexPlan",
    "IndexReport",
    "LatencySummary",
    "PartitionIndex",
    "RetrievalMode",
    "RetrievedChunk",
    "SearchResult",
]

RetrievalMode = Literal["vector", "hybrid"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievedChunk(_Frozen):
    """One chunk returned by ``chronofence_search``.

    ``published_at`` is carried out of the database rather than dropped so the
    caller can re-assert the time lock without trusting it. The leakage suite
    does exactly that: a property test over the full retrieval trace checks
    ``published_at < as_of`` on every row that ever reached an agent, which
    would catch a regression in the SQL that the SQL's own tests missed.
    """

    chunk_id: str
    document_id: str
    ordinal: int
    body: str
    published_at: datetime
    source: str
    url: str
    title: str
    distance: float
    # Why this chunk was chosen, on the hybrid path; all None on the vector
    # path, where `distance` is the whole story. Carried for the same reason
    # `published_at` is: §11.2 walks from an outcome back to its evidence, and
    # "it was the keyword pool's first hit and the vector pool never saw it"
    # is a different explanation from "it was nearest".
    vector_rank: int | None = None
    keyword_rank: int | None = None
    recency_rank: int | None = None
    fused_score: float | None = None


class HybridCandidate(_Frozen):
    """One row of ``chronofence_search_hybrid``: a candidate, not yet a result.

    ``vector_rank`` and ``keyword_rank`` are the 1-based position each pool
    gave the row, ``None`` where that pool did not supply it. At least one is
    always set -- a row in neither pool has no reason to be in the union, and
    accepting one would let a malformed result be fused as though it were
    evidence.

    ``simhash`` is the parent document's fingerprint in Postgres's signed
    ``bigint`` form (migration 003); only its bit pattern is ever compared.
    """

    chunk_id: str
    document_id: str
    ordinal: int
    body: str
    published_at: datetime
    source: str
    url: str
    title: str
    distance: float
    vector_rank: int | None = Field(default=None, ge=1)
    keyword_rank: int | None = Field(default=None, ge=1)
    terms_matched: int | None = Field(default=None, ge=1)
    simhash: int

    @model_validator(mode="after")
    def _belongs_to_a_pool(self) -> HybridCandidate:
        if self.vector_rank is None and self.keyword_rank is None:
            raise ValueError(
                f"candidate {self.chunk_id!r} carries neither a vector rank nor a keyword "
                "rank; the union holds only rows a pool supplied"
            )
        if self.published_at.tzinfo is None:
            raise ValueError(
                f"candidate {self.chunk_id!r} has a naive published_at; recency cannot "
                "be ranked across naive and aware timestamps"
            )
        return self


class SearchResult(_Frozen):
    """The chunks one query returned, with the cutoff they were taken under."""

    as_of: datetime
    k: int
    chunks: tuple[RetrievedChunk, ...]
    elapsed_ms: float
    mode: RetrievalMode = "vector"
    # Hybrid only: the entity terms the keyword pool was asked for, and how
    # many candidates the two pools supplied between them. A result that says
    # "hybrid" with no terms was, in effect, the vector pool re-ranked by
    # recency -- worth being able to see afterwards.
    terms: tuple[str, ...] = ()
    candidates: int | None = None

    @property
    def chunk_ids(self) -> tuple[str, ...]:
        return tuple(chunk.chunk_id for chunk in self.chunks)

    def latest_published_at(self) -> datetime | None:
        """The newest evidence returned, or None when nothing was."""
        if not self.chunks:
            return None
        return max(chunk.published_at for chunk in self.chunks)


class PartitionIndex(_Frozen):
    """Measured state of one ``chunks`` partition and its HNSW index.

    ``m`` and ``ef_construction`` are the index's *build* parameters. Unlike
    IVFFlat's ``lists`` they do not depend on the row count, which is why
    ADR-0026 retires the rebuild-on-drift pass: an HNSW index that exists stays
    correct as rows arrive.
    """

    partition: str
    rows: int
    index_name: str | None
    m: int | None
    ef_construction: int | None
    # The full-text GIN index the hybrid keyword pool needs (migration 018).
    # Defaulted so the HNSW planning rule, which does not read it, can be
    # exercised without restating it.
    fts_index_name: str | None = None


IndexAction = str  # one of: "create", "rebuild", "keep", "skip-empty"


class IndexPlan(_Frozen):
    """What ``cascade retrieval index`` intends to do to one partition.

    Separated from execution so the planning rule is a pure function of
    measured state and can be tested without a database.
    """

    partition: str
    rows: int
    current_m: int | None
    current_ef_construction: int | None
    target_m: int
    target_ef_construction: int
    action: IndexAction
    reason: str


class IndexReport(_Frozen):
    """What it actually did."""

    plans: tuple[IndexPlan, ...]
    created: int
    rebuilt: int
    kept: int
    skipped_empty: int
    elapsed_s: float


class FtsIndexPlan(_Frozen):
    """What ``cascade retrieval index --fts`` intends for one partition.

    There is no ``rebuild``: a GIN index over ``to_tsvector('english', body)``
    has no build parameter this project sets, so an index that exists is the
    index that is wanted. Changing the text-search configuration would change
    the *expression*, and the partition view matches on the expression, so
    such an index would simply read as absent and plan as ``create``.
    """

    partition: str
    rows: int
    index_name: str
    action: IndexAction  # "create", "keep" or "skip-empty"
    reason: str


class FtsIndexReport(_Frozen):
    """What the full-text pass actually did."""

    plans: tuple[FtsIndexPlan, ...]
    created: int
    kept: int
    skipped_empty: int
    elapsed_s: float


class LatencySummary(_Frozen):
    """Latency percentiles over a bench run, in milliseconds.

    Percentiles are reported, never a mean. A mean latency hides exactly the
    tail the 15 ms budget is about.
    """

    n: int = Field(ge=0)
    p50: float
    p95: float
    p99: float
    minimum: float
    maximum: float
    histogram: tuple[tuple[float, float, int], ...]
    """(lower_ms, upper_ms, count) buckets, contiguous and ordered."""

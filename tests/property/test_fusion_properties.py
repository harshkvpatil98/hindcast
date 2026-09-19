"""Fusion is a function of the candidate *set* (M14).

Rows come back from Postgres in whatever order the executor produced them. The
function orders by ``chunk_id`` today; a future plan, a different driver, or a
caller that merges two result sets need not. If the order rows arrive in could
reach the fused ranking, two replays of one run could hand an agent different
evidence -- and the event-log hash (M8) would diverge for a reason nobody
would look for in retrieval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from cascade.retrieval.fusion import FusionParams, fuse, select
from cascade.retrieval.schema import HybridCandidate

PARAMS = FusionParams(
    rrf_k=60,
    vector_weight=1.0,
    keyword_weight=1.0,
    recency_weight=0.5,
    pool=200,
    max_per_story=2,
    simhash_bits=8,
)
EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


@st.composite
def candidate_sets(draw: st.DrawFn) -> list[HybridCandidate]:
    """Unions shaped like the SQL's: unique ids, each row in at least one pool.

    Timestamps, documents and fingerprints are drawn from small ranges on
    purpose. Ties are where order-dependence hides, and a strategy that almost
    never produced one would prove the property only for the easy case.
    """
    size = draw(st.integers(min_value=0, max_value=40))
    rows: list[HybridCandidate] = []
    for index in range(size):
        membership = draw(st.sampled_from(["vector", "keyword", "both"]))
        rows.append(
            HybridCandidate(
                chunk_id=f"chunk-{index:03d}",
                document_id=f"doc-{draw(st.integers(min_value=0, max_value=8))}",
                ordinal=draw(st.integers(min_value=0, max_value=3)),
                body="text",
                published_at=EPOCH - timedelta(days=draw(st.integers(min_value=0, max_value=5))),
                source="ccnews",
                url="",
                title="",
                distance=draw(st.floats(min_value=0.0, max_value=2.0, allow_nan=False)),
                vector_rank=(
                    draw(st.integers(min_value=1, max_value=200))
                    if membership in {"vector", "both"}
                    else None
                ),
                keyword_rank=(
                    draw(st.integers(min_value=1, max_value=200))
                    if membership in {"keyword", "both"}
                    else None
                ),
                terms_matched=1 if membership in {"keyword", "both"} else None,
                simhash=draw(st.sampled_from([0, 1, 3, 0xFFFF, -1, 1 << 40])),
            )
        )
    return rows


@given(rows=candidate_sets(), data=st.data())
@settings(max_examples=300, deadline=None)
def test_fusion_is_invariant_under_permutation_of_its_input(
    rows: list[HybridCandidate], data: st.DataObject
) -> None:
    shuffled = data.draw(st.permutations(rows))
    assert fuse(rows, PARAMS) == fuse(shuffled, PARAMS)


@given(rows=candidate_sets(), data=st.data(), k=st.integers(min_value=1, max_value=60))
@settings(max_examples=300, deadline=None)
def test_selection_is_invariant_under_permutation_of_its_input(
    rows: list[HybridCandidate], data: st.DataObject, k: int
) -> None:
    """The whole path -- fusion and the diversity pass, which is stateful and
    walks in order, so it is the half more likely to leak an ordering."""
    shuffled = data.draw(st.permutations(rows))
    assert select(rows, k=k, params=PARAMS) == select(shuffled, k=k, params=PARAMS)


@given(rows=candidate_sets())
@settings(max_examples=200, deadline=None)
def test_fusion_returns_a_permutation_of_its_input(rows: list[HybridCandidate]) -> None:
    """Recency is a ranking, not a filter: nothing handed to `fuse` goes missing."""
    fused = fuse(rows, PARAMS)
    assert sorted(entry.candidate.chunk_id for entry in fused) == sorted(
        row.chunk_id for row in rows
    )


@given(rows=candidate_sets())
@settings(max_examples=200, deadline=None)
def test_the_fused_order_is_total(rows: list[HybridCandidate]) -> None:
    """Best first, ties on chunk_id: no two adjacent entries are out of order."""
    fused = fuse(rows, PARAMS)
    keys = [(-entry.score, entry.candidate.chunk_id) for entry in fused]
    assert keys == sorted(keys)


@given(rows=candidate_sets(), k=st.integers(min_value=1, max_value=60))
@settings(max_examples=200, deadline=None)
def test_selection_fills_k_whenever_the_union_can(rows: list[HybridCandidate], k: int) -> None:
    """Diversity never returns fewer chunks than exist, and never invents one."""
    chosen = select(rows, k=k, params=PARAMS)
    ids = [entry.candidate.chunk_id for entry in chosen]
    assert len(ids) == min(k, len(rows))
    assert len(set(ids)) == len(ids)
    assert set(ids) <= {row.chunk_id for row in rows}

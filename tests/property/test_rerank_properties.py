"""Reranking is a permutation, quantified over generated input (ADR-0047, M15).

`tests/unit/test_retrieval_rerank.py` holds the cases a person can check on
paper. What is here is the claim the security argument actually rests on,
stated as a property and tested against inputs chosen to be awkward: whatever
scores a reranker returns, the result is a truncated permutation of the rows
it was handed, and nothing about those rows changes.

ADR-0030 refused a managed knowledge base because it would replace the
`published_at < as_of` filter with a vendor's promise. ADR-0047 admits a
managed *reranker* on the grounds that it permutes a set the database already
filtered. That distinction is only worth anything if the permutation property
holds for every reranker, including a broken or hostile one -- so the scores
here are drawn adversarially: duplicates, negative zero, subnormals, and
values that collide at every bit but the last.

Ties are the interesting case throughout. Equal scores are where an
order-dependent implementation hides, and M8's criterion is a byte-identical
event-log hash across processes: two equally ranked chunks that swap between
replays would hand an agent different evidence for a reason nobody would look
for in retrieval.
"""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from cascade.retrieval.rerank import apply_scores
from cascade.retrieval.schema import RetrievedChunk

EPOCH = datetime(2024, 1, 1, tzinfo=UTC)

# Drawn to collide: a handful of distinct magnitudes, both zeros, and values
# a fraction apart. A strategy over the whole float line would almost never
# produce a tie, and would prove the property only for the easy case.
scores = st.one_of(
    st.sampled_from([0.0, -0.0, 1.0, -1.0, 0.5, 1e-300, 5e-324]),
    st.floats(min_value=-2.0, max_value=2.0, allow_nan=False, allow_infinity=False),
)


@st.composite
def pools(draw: st.DrawFn) -> tuple[list[RetrievedChunk], list[float]]:
    """A retrieved pool with unique ids, and one score per row.

    Ids are drawn from a small alphabet so that lexicographic tie-breaking is
    exercised rather than merely present, and documents repeat so that two
    chunks of one story can carry equal scores.
    """
    size = draw(st.integers(min_value=0, max_value=12))
    ids = draw(
        st.lists(
            st.text(alphabet="abc", min_size=1, max_size=3),
            min_size=size,
            max_size=size,
            unique=True,
        )
    )
    chunks = [
        RetrievedChunk(
            chunk_id=chunk_id,
            document_id=draw(st.sampled_from(["doc-1", "doc-2", "doc-3"])),
            ordinal=draw(st.integers(min_value=0, max_value=4)),
            body=draw(st.text(max_size=20)),
            published_at=EPOCH - timedelta(days=draw(st.integers(min_value=0, max_value=400))),
            source="ccnews",
            url="",
            title="",
            distance=draw(st.floats(min_value=0.0, max_value=2.0)),
            vector_rank=index + 1,
            fused_score=1.0 - index / 100,
        )
        for index, chunk_id in enumerate(ids)
    ]
    values = draw(st.lists(scores, min_size=size, max_size=size))
    return chunks, values


@given(pools(), st.integers(min_value=0, max_value=20))
@settings(max_examples=150, deadline=None)
def test_the_result_is_always_drawn_from_the_pool(
    pool: tuple[list[RetrievedChunk], list[float]], top_k: int
) -> None:
    """The claim ADR-0047 rests on: a reranker cannot introduce a row.

    If this can fail, then a compromised reranker could surface a chunk the
    time-locked SQL never returned, and the managed-service argument
    collapses back to ADR-0030's refusal.
    """
    chunks, values = pool
    result = apply_scores(chunks, values, top_k=top_k)
    assert {chunk.chunk_id for chunk in result} <= {chunk.chunk_id for chunk in chunks}


@given(pools(), st.integers(min_value=0, max_value=20))
@settings(max_examples=150, deadline=None)
def test_nothing_is_duplicated_or_silently_dropped(
    pool: tuple[list[RetrievedChunk], list[float]], top_k: int
) -> None:
    """Exactly ``min(top_k, len(pool))`` distinct rows come back.

    A duplicate would let one chunk occupy two evidence slots; a short result
    would silently narrow the evidence without the caller's k changing.
    """
    chunks, values = pool
    result = apply_scores(chunks, values, top_k=top_k)
    ids = [chunk.chunk_id for chunk in result]
    assert len(ids) == min(top_k, len(chunks))
    assert len(set(ids)) == len(ids)


@given(pools(), st.integers(min_value=0, max_value=20))
@settings(max_examples=150, deadline=None)
def test_the_rows_themselves_are_untouched(
    pool: tuple[list[RetrievedChunk], list[float]], top_k: int
) -> None:
    """Reranking reorders evidence; it must not edit it.

    The leakage suite re-asserts ``published_at < as_of`` over the retrieval
    trace on the caller's side. If a body or a date could change here, that
    re-assertion would be checking a copy rather than the row the database
    returned.
    """
    chunks, values = pool
    result = apply_scores(chunks, values, top_k=top_k)
    original = {chunk.chunk_id: chunk for chunk in chunks}
    mutable = {"rerank_rank", "rerank_score"}
    for chunk in result:
        assert chunk.model_dump(exclude=mutable) == original[chunk.chunk_id].model_dump(
            exclude=mutable
        )


@given(pools(), st.integers(min_value=0, max_value=20))
@settings(max_examples=150, deadline=None)
def test_the_ordering_is_total_and_breaks_ties_on_chunk_id(
    pool: tuple[list[RetrievedChunk], list[float]], top_k: int
) -> None:
    """Equal scores order by ``chunk_id``, so a replay cannot reorder them.

    Stated as a pairwise check rather than by recomputing the sort, so that a
    bug shared between the implementation and the test cannot hide it.
    """
    chunks, values = pool
    result = apply_scores(chunks, values, top_k=top_k)
    for earlier, later in itertools.pairwise(result):
        assert earlier.rerank_score is not None and later.rerank_score is not None
        if earlier.rerank_score == later.rerank_score:
            assert earlier.chunk_id < later.chunk_id
        else:
            assert earlier.rerank_score > later.rerank_score


@given(pools(), st.integers(min_value=0, max_value=20), st.randoms(use_true_random=False))
@settings(max_examples=150, deadline=None)
def test_the_result_does_not_depend_on_the_order_the_pool_arrived_in(
    pool: tuple[list[RetrievedChunk], list[float]],
    top_k: int,
    rng: object,
) -> None:
    """The same rows with the same scores rank the same, however they arrive.

    Rows reach Python in whatever order the executor produced them. If arrival
    order could reach the ranking, two replays of one run could hand an agent
    different evidence and the M8 hash would diverge for a reason nobody would
    look for in a reranker.
    """
    chunks, values = pool
    paired = list(zip(chunks, values, strict=True))
    shuffled = list(paired)
    rng.shuffle(shuffled)  # type: ignore[attr-defined]

    first = apply_scores(chunks, values, top_k=top_k)
    second = apply_scores(
        [chunk for chunk, _ in shuffled], [value for _, value in shuffled], top_k=top_k
    )
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]


@given(pools())
@settings(max_examples=100, deadline=None)
def test_a_reranker_with_nothing_to_say_falls_through_to_the_id_order(
    pool: tuple[list[RetrievedChunk], list[float]],
) -> None:
    """All-equal scores must not reshuffle: the tie-break decides, and it is total.

    This is the shape `LexicalReranker` produces for an empty query, and it is
    the one case where a reranker must be provably inert rather than merely
    harmless.
    """
    chunks, _ = pool
    result = apply_scores(chunks, [0.0] * len(chunks), top_k=len(chunks))
    assert [chunk.chunk_id for chunk in result] == sorted(chunk.chunk_id for chunk in chunks)


@given(pools())
@settings(max_examples=100, deadline=None)
def test_negative_zero_and_zero_are_one_score_not_two(
    pool: tuple[list[RetrievedChunk], list[float]],
) -> None:
    """``-0.0 == 0.0`` in IEEE 754, so they must tie rather than order.

    Negating for a descending sort turns 0.0 into -0.0 and back; an
    implementation that compared bit patterns, or sorted on a key that
    distinguished them, would order two equal scores by their sign bit.
    """
    chunks, _ = pool
    alternating = [(-0.0 if index % 2 else 0.0) for index in range(len(chunks))]
    result = apply_scores(chunks, alternating, top_k=len(chunks))
    assert [chunk.chunk_id for chunk in result] == sorted(chunk.chunk_id for chunk in chunks)
    assert all(
        chunk.rerank_score is not None and math.copysign(1.0, chunk.rerank_score) in (1.0, -1.0)
        for chunk in result
    )

"""Reranking as a permutation of a time-locked pool (ADR-0047, M15).

The claim this file holds is narrow and load-bearing: whatever a reranker
returns, the result is built out of the rows that were handed in. The guard is
the *interface* -- a reranker gets text and returns numbers -- so these tests
exercise the one function that turns numbers into an ordering, including with
a reranker that is actively trying to misbehave.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from cascade.retrieval.rerank import (
    LexicalReranker,
    RerankError,
    apply_scores,
    rerank_cache_key,
    tokenize,
)
from cascade.retrieval.schema import RetrievedChunk

NEWEST = datetime(2025, 6, 1, tzinfo=UTC)


def pool(*chunk_ids: str, bodies: dict[str, str] | None = None) -> tuple[RetrievedChunk, ...]:
    """A retrieved pool in rank order: the first argument is rank 1."""
    bodies = bodies or {}
    return tuple(
        RetrievedChunk(
            chunk_id=chunk_id,
            document_id=f"doc-{chunk_id}",
            ordinal=0,
            body=bodies.get(chunk_id, f"body of {chunk_id}"),
            published_at=NEWEST - timedelta(days=index),
            source="ccnews",
            url="",
            title="",
            distance=0.5,
            vector_rank=index + 1,
            fused_score=1.0 - index / 100,
        )
        for index, chunk_id in enumerate(chunk_ids)
    )


def ids(chunks: tuple[RetrievedChunk, ...]) -> list[str]:
    return [chunk.chunk_id for chunk in chunks]


class TestApplyScoresIsAPermutation:
    def test_the_result_is_drawn_from_the_input_pool(self) -> None:
        given = pool("a", "b", "c")
        result = apply_scores(given, [0.1, 0.9, 0.5], top_k=3)
        assert ids(result) == ["b", "c", "a"]
        assert set(ids(result)) <= set(ids(given))

    def test_every_field_but_the_rerank_pair_survives_unchanged(self) -> None:
        # A reranker reorders evidence; it must not edit it. If a body or a
        # published_at could change here, the leakage suite's re-assertion of
        # the time lock over the retrieval trace would be checking a copy.
        given = pool("a", "b")
        result = apply_scores(given, [0.1, 0.9], top_k=2)
        before = {chunk.chunk_id: chunk for chunk in given}
        for chunk in result:
            original = before[chunk.chunk_id]
            assert chunk.model_dump(exclude={"rerank_rank", "rerank_score"}) == original.model_dump(
                exclude={"rerank_rank", "rerank_score"}
            )

    def test_truncation_keeps_the_best_and_drops_the_rest(self) -> None:
        result = apply_scores(pool("a", "b", "c", "d"), [0.1, 0.9, 0.5, 0.7], top_k=2)
        assert ids(result) == ["b", "d"]

    def test_top_k_above_the_pool_returns_the_whole_pool(self) -> None:
        assert ids(apply_scores(pool("a", "b"), [0.1, 0.2], top_k=50)) == ["b", "a"]

    def test_top_k_of_zero_returns_nothing(self) -> None:
        assert apply_scores(pool("a", "b"), [0.1, 0.2], top_k=0) == ()

    def test_the_displacement_is_readable_from_the_result(self) -> None:
        # "d" arrived fourth (vector_rank 4) and left first (rerank_rank 1).
        # Both ranks ride on the row, so a provenance walk can say so without
        # running the query a second time.
        result = apply_scores(pool("a", "b", "c", "d"), [0.1, 0.2, 0.3, 0.9], top_k=4)
        assert [(c.chunk_id, c.vector_rank, c.rerank_rank) for c in result] == [
            ("d", 4, 1),
            ("c", 3, 2),
            ("b", 2, 3),
            ("a", 1, 4),
        ]

    def test_an_unreranked_chunk_carries_no_rerank_fields(self) -> None:
        # The absence is the signal: a stored chunk with rerank_rank None was
        # never reranked, and cannot be mistaken for one the reranker ranked
        # first.
        assert all(chunk.rerank_rank is None for chunk in pool("a", "b"))


class TestDeterminism:
    def test_ties_break_on_chunk_id_not_on_arrival_order(self) -> None:
        # M8 hashes the event log byte for byte: two equally relevant chunks
        # must not swap between replays.
        assert ids(apply_scores(pool("b", "a", "c"), [0.5, 0.5, 0.5], top_k=3)) == ["a", "b", "c"]

    def test_the_same_pool_in_a_different_order_yields_the_same_ranking(self) -> None:
        first = apply_scores(pool("a", "b", "c"), [0.1, 0.9, 0.5], top_k=3)
        second = apply_scores(pool("c", "a", "b"), [0.5, 0.1, 0.9], top_k=3)
        assert ids(first) == ids(second)

    def test_scores_are_carried_through_unmodified(self) -> None:
        result = apply_scores(pool("a", "b"), [0.25, 0.75], top_k=2)
        assert [chunk.rerank_score for chunk in result] == [0.75, 0.25]


class TestAMisbehavingRerankerIsRefused:
    def test_too_few_scores_is_an_error_not_a_partial_ranking(self) -> None:
        with pytest.raises(RerankError, match=r"2 score.* for 3 candidate"):
            apply_scores(pool("a", "b", "c"), [0.1, 0.2], top_k=3)

    def test_too_many_scores_is_an_error(self) -> None:
        with pytest.raises(RerankError, match=r"4 score.* for 2 candidate"):
            apply_scores(pool("a", "b"), [0.1, 0.2, 0.3, 0.4], top_k=2)

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_a_non_finite_score_is_an_error_not_a_sort_key(self, bad: float) -> None:
        # NaN in a sort key silently produces an arbitrary order rather than
        # an exception, which is the failure mode worth refusing.
        with pytest.raises(RerankError, match="non-finite score"):
            apply_scores(pool("a", "b"), [0.5, bad], top_k=2)

    def test_a_negative_top_k_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="top_k must not be negative"):
            apply_scores(pool("a"), [0.1], top_k=-1)


class TestCacheKey:
    def test_the_same_call_keys_the_same(self) -> None:
        args: dict[str, object] = {
            "model_id": "m",
            "query": "q",
            "chunk_ids": ["a", "b"],
            "top_k": 5,
        }
        assert rerank_cache_key(**args) == rerank_cache_key(**args)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "changed",
        [
            {"model_id": "other"},
            {"query": "different"},
            {"chunk_ids": ["a", "c"]},
            {"top_k": 6},
        ],
    )
    def test_every_component_changes_the_key(self, changed: dict[str, object]) -> None:
        base: dict[str, object] = {
            "model_id": "m",
            "query": "q",
            "chunk_ids": ["a", "b"],
            "top_k": 5,
        }
        assert rerank_cache_key(**base) != rerank_cache_key(**{**base, **changed})  # type: ignore[arg-type]

    def test_pool_order_changes_the_key(self) -> None:
        # A reranker is permitted to be position-sensitive, so two orders of
        # the same pool are two calls and must not share a recording.
        first = rerank_cache_key(model_id="m", query="q", chunk_ids=["a", "b"], top_k=5)
        second = rerank_cache_key(model_id="m", query="q", chunk_ids=["b", "a"], top_k=5)
        assert first != second

    def test_the_separator_prevents_a_field_boundary_collision(self) -> None:
        # Without a delimiter, ("ab", "c") and ("a", "bc") would hash alike.
        assert rerank_cache_key(
            model_id="ab", query="c", chunk_ids=[], top_k=1
        ) != rerank_cache_key(model_id="a", query="bc", chunk_ids=[], top_k=1)


class TestTokenize:
    def test_lowercases_and_splits_on_non_alphanumerics(self) -> None:
        assert tokenize("The FTC's Merger-Review, 2024!") == (
            "the",
            "ftc",
            "s",
            "merger",
            "review",
            "2024",
        )

    def test_is_stable(self) -> None:
        assert tokenize("a b a") == tokenize("a b a")


class TestLexicalReranker:
    def test_a_document_carrying_the_query_terms_outranks_one_that_does_not(self) -> None:
        scores = LexicalReranker().score(
            query="merger review",
            documents=[
                "an unrelated article about weather patterns",
                "the merger review concluded this week",
            ],
        )
        assert scores[1] > scores[0]

    def test_an_empty_query_scores_everything_zero(self) -> None:
        # A reranker with nothing to say must not reshuffle: all-equal scores
        # fall through to the chunk_id tie-break in `apply_scores`.
        assert list(LexicalReranker().score(query="", documents=["one", "two"])) == [0.0, 0.0]

    def test_an_empty_pool_is_not_an_error(self) -> None:
        assert list(LexicalReranker().score(query="anything", documents=[])) == []

    def test_a_term_in_every_document_cannot_penalise_the_one_that_repeats_it(self) -> None:
        # The +1 inside the IDF log keeps a ubiquitous term at ~0 rather than
        # negative, which would make repetition actively harmful.
        scores = LexicalReranker().score(
            query="merger", documents=["merger", "merger merger merger"]
        )
        assert all(score >= 0.0 for score in scores)

    def test_scoring_is_independent_of_the_order_the_pool_arrives_in(self) -> None:
        reranker = LexicalReranker()
        documents = ["the merger review", "unrelated weather", "merger merger review"]
        forward = list(reranker.score(query="merger review", documents=documents))
        backward = list(reranker.score(query="merger review", documents=list(reversed(documents))))
        assert forward == list(reversed(backward))

    def test_scoring_is_bit_identical_across_calls(self) -> None:
        reranker = LexicalReranker()
        documents = ["alpha beta", "beta gamma delta", "alpha alpha gamma"]
        assert list(reranker.score(query="alpha gamma", documents=documents)) == list(
            reranker.score(query="alpha gamma", documents=documents)
        )

    def test_it_satisfies_the_reranker_protocol_end_to_end(self) -> None:
        given = pool(
            "a",
            "b",
            bodies={"a": "unrelated weather", "b": "the merger review concluded"},
        )
        reranker = LexicalReranker()
        scores = reranker.score(query="merger", documents=[chunk.body for chunk in given])
        assert ids(apply_scores(given, scores, top_k=2)) == ["b", "a"]
        assert reranker.model_id == "bm25-local-v1"

"""Reciprocal-rank fusion and the diversity pass (M14).

Small cases worked by hand, so a failure says which arithmetic changed. The
order-invariance and permutation properties are quantified over generated
input in ``tests/property/test_fusion_properties.py``; what is here are the
cases a person can check on paper.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cascade.config import Settings
from cascade.corpus.simhash import from_signed, hamming, simhash, to_signed
from cascade.retrieval.fusion import (
    FusionParams,
    diversify,
    fuse,
    hamming64,
    recency_ranks,
    recency_weight_bound,
    select,
)
from cascade.retrieval.schema import HybridCandidate
from cascade.retrieval.search import fusion_params

NEWEST = datetime(2025, 6, 1, tzinfo=UTC)


def params(**overrides: float | int) -> FusionParams:
    base: dict[str, float | int] = {
        "rrf_k": 60,
        "vector_weight": 1.0,
        "keyword_weight": 1.0,
        "recency_weight": 0.5,
        "pool": 200,
        "max_per_story": 2,
        "simhash_bits": 8,
    }
    base.update(overrides)
    return FusionParams(**base)  # type: ignore[arg-type]


def candidate(
    chunk_id: str,
    *,
    vector_rank: int | None = None,
    keyword_rank: int | None = None,
    days_old: float = 0.0,
    document_id: str | None = None,
    ordinal: int = 0,
    simhash_value: int = 0,
) -> HybridCandidate:
    return HybridCandidate(
        chunk_id=chunk_id,
        document_id=document_id if document_id is not None else f"doc-{chunk_id}",
        ordinal=ordinal,
        body=f"body of {chunk_id}",
        published_at=NEWEST - timedelta(days=days_old),
        source="ccnews",
        url="",
        title="",
        distance=0.5,
        vector_rank=vector_rank,
        keyword_rank=keyword_rank,
        terms_matched=None if keyword_rank is None else 1,
        simhash=simhash_value,
    )


def order(fused: object) -> list[str]:
    return [entry.candidate.chunk_id for entry in fused]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


class TestScores:
    def test_a_score_is_the_sum_of_its_three_reciprocal_ranks(self) -> None:
        """Worked by hand: vector 1, keyword 3, and the only candidate, so recency 1.

        1/(60+1) + 1/(60+3) + 0.5/(60+1)
        """
        (entry,) = fuse([candidate("a", vector_rank=1, keyword_rank=3)], params())
        assert entry.score == pytest.approx(1 / 61 + 1 / 63 + 0.5 / 61)
        assert entry.recency_rank == 1

    def test_weights_scale_their_own_term_only(self) -> None:
        (entry,) = fuse(
            [candidate("a", vector_rank=2, keyword_rank=5)],
            params(vector_weight=2.0, keyword_weight=3.0, recency_weight=0.25),
        )
        assert entry.score == pytest.approx(2 / 62 + 3 / 65 + 0.25 / 61)

    def test_agreement_beats_a_single_list_even_at_its_top(self) -> None:
        """The point of fusing: two lists agreeing on a chunk at rank 50 outweigh
        one list putting another first. 2/110 = 0.01818 > 1/61 = 0.01639."""
        fused = fuse(
            [
                candidate("agreed", vector_rank=50, keyword_rank=50),
                candidate("vector-top", vector_rank=1),
            ],
            params(recency_weight=0.0),
        )
        assert order(fused) == ["agreed", "vector-top"]

    def test_a_three_candidate_case_in_full(self) -> None:
        """All three same age, so recency adds the same to each and cannot reorder.

        a: 1/61 + 1/63 = 0.032266   b: 1/62 + 1/61 = 0.032522   c: 1/63 + 1/62 = 0.032002
        """
        fused = fuse(
            [
                candidate("a", vector_rank=1, keyword_rank=3),
                candidate("b", vector_rank=2, keyword_rank=1),
                candidate("c", vector_rank=3, keyword_rank=2),
            ],
            params(),
        )
        assert order(fused) == ["b", "a", "c"]
        assert [entry.recency_rank for entry in fused] == [1, 1, 1]


class TestSingleListCandidates:
    def test_a_candidate_in_only_the_keyword_list_still_ranks(self) -> None:
        """Absence contributes nothing; it does not disqualify. This is the case
        the keyword pool exists for -- the chunk the embedder never surfaced."""
        fused = fuse(
            [candidate("keyword-only", keyword_rank=1), candidate("vector-only", vector_rank=9)],
            params(),
        )
        assert order(fused) == ["keyword-only", "vector-only"]
        assert all(entry.score > 0 for entry in fused)

    def test_a_candidate_in_only_the_vector_list_still_ranks(self) -> None:
        fused = fuse(
            [candidate("keyword-only", keyword_rank=9), candidate("vector-only", vector_rank=1)],
            params(),
        )
        assert order(fused) == ["vector-only", "keyword-only"]

    def test_the_two_lists_are_symmetric_at_equal_weight(self) -> None:
        """Rank 1 in either list is worth the same; the tie falls to chunk_id."""
        fused = fuse([candidate("b", keyword_rank=1), candidate("a", vector_rank=1)], params())
        assert fused[0].score == fused[1].score
        assert order(fused) == ["a", "b"]

    def test_a_candidate_in_neither_list_cannot_be_constructed(self) -> None:
        with pytest.raises(ValueError, match="neither a vector rank nor a keyword rank"):
            candidate("orphan")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_equal_scores_are_ordered_by_chunk_id(self) -> None:
        tied = [candidate(name, vector_rank=1) for name in ("m", "c", "x", "a")]
        assert order(fuse(tied, params())) == ["a", "c", "m", "x"]

    def test_the_order_rows_arrived_in_cannot_reach_the_result(self) -> None:
        rows = [
            candidate("a", vector_rank=1, days_old=300),
            candidate("b", vector_rank=2, keyword_rank=1, days_old=2),
            candidate("c", keyword_rank=2, days_old=2),
            candidate("d", vector_rank=3, days_old=40),
        ]
        forwards = fuse(rows, params())
        backwards = fuse(list(reversed(rows)), params())
        assert forwards == backwards

    def test_a_repeated_chunk_is_refused(self) -> None:
        """Ranks over a multiset are not well defined, and the SQL's FULL JOIN
        guarantees one row per chunk -- so a repeat means that guarantee broke."""
        with pytest.raises(ValueError, match="appears twice"):
            fuse([candidate("a", vector_rank=1), candidate("a", keyword_rank=1)], params())

    def test_an_empty_union_fuses_to_nothing(self) -> None:
        assert fuse([], params()) == ()
        assert select([], k=6, params=params()) == ()


# ---------------------------------------------------------------------------
# Recency is a ranking, not a filter
# ---------------------------------------------------------------------------


class TestRecency:
    def test_recency_ranks_are_competition_ranks_over_timestamps(self) -> None:
        """Chunks of one article share a timestamp and so a rank: 1, 1, 3, 4."""
        ranks = recency_ranks(
            [
                candidate("new-1", vector_rank=1, days_old=0),
                candidate("new-2", vector_rank=2, days_old=0),
                candidate("mid", vector_rank=3, days_old=10),
                candidate("old", vector_rank=4, days_old=900),
            ]
        )
        assert ranks == {"new-1": 1, "new-2": 1, "mid": 3, "old": 4}

    def test_recency_breaks_a_tie_between_equally_relevant_chunks(self) -> None:
        """What recency is for: same relevance, the fresher one first."""
        fused = fuse(
            [
                candidate("stale", vector_rank=1, days_old=500),
                candidate("fresh", keyword_rank=1, days_old=1),
            ],
            params(),
        )
        assert order(fused) == ["fresh", "stale"]

    def test_the_oldest_candidate_is_never_dropped(self) -> None:
        """A filter removes; a ranking reorders. Every candidate handed to `fuse`
        comes back, however old -- here eight years older than the rest."""
        rows = [candidate(f"new-{i}", vector_rank=i + 2, days_old=i) for i in range(30)]
        rows.append(candidate("ancient", vector_rank=1, days_old=8 * 365))
        fused = fuse(rows, params())
        assert sorted(order(fused)) == sorted(row.chunk_id for row in rows)

    def test_old_background_that_is_strongly_relevant_still_wins(self) -> None:
        """First in both relevance lists and the oldest thing in the union by
        eight years: it must still come first against newer, weaker chunks."""
        rows = [
            candidate(f"new-{i}", vector_rank=i + 10, days_old=i, document_id=f"d{i}")
            for i in range(40)
        ]
        rows.append(candidate("background", vector_rank=1, keyword_rank=1, days_old=8 * 365))
        assert order(fuse(rows, params()))[0] == "background"
        assert "background" in order(select(rows, k=6, params=params()))

    def test_a_top_vector_hit_is_not_excluded_for_being_oldest(self) -> None:
        """The brief's case. The vector pool's first hit is the oldest of a full
        200-row pool whose every other member is newer; the six newest sit at
        the bottom of the pool. It must survive to the top six -- recency may
        not promote the pool's tail over its head."""
        rows = [candidate("top-hit", vector_rank=1, days_old=3000)]
        rows += [
            candidate(f"v{rank:03d}", vector_rank=rank, days_old=200 - rank)
            for rank in range(2, 201)
        ]
        chosen = order(select(rows, k=6, params=params()))
        assert "top-hit" in chosen

    def test_the_bound_is_what_makes_that_true(self) -> None:
        """At the bound the pool's newest tail ties its oldest head; above it, the
        tail wins. Worked for rrf_k 60, pool 200: 1 - 61/260 = 0.76538."""
        bound = recency_weight_bound(rrf_k=60, pool=200, relevance_weight=1.0)
        assert bound == pytest.approx(1 - 61 / 260)

        def head_beats_tail(weight: float) -> bool:
            # Built without FusionParams' refusal, to look past the bound.
            head = 1 / 61 + weight / (60 + 10_000)
            tail = 1 / 260 + weight / 61
            return head > tail

        assert head_beats_tail(bound - 0.01)
        assert not head_beats_tail(bound + 0.01)

    def test_a_recency_weight_at_or_above_the_bound_is_refused(self) -> None:
        with pytest.raises(ValueError, match="break ties, not overrule relevance"):
            params(recency_weight=0.77)
        with pytest.raises(ValueError, match="break ties, not overrule relevance"):
            params(recency_weight=1.0)
        params(recency_weight=0.76)  # below: accepted

    def test_the_bound_follows_the_weaker_relevance_list(self) -> None:
        """Recency must not overrule *either* list, so the smaller weight binds."""
        with pytest.raises(ValueError):
            params(keyword_weight=0.5, recency_weight=0.5)

    def test_zero_recency_weight_switches_it_off(self) -> None:
        fused = fuse(
            [
                candidate("stale", vector_rank=1, days_old=500),
                candidate("fresh", vector_rank=2, days_old=1),
            ],
            params(recency_weight=0.0),
        )
        assert order(fused) == ["stale", "fresh"]


class TestParams:
    @pytest.mark.parametrize(
        "bad",
        [
            {"rrf_k": 0},
            {"vector_weight": 0.0},
            {"keyword_weight": -1.0},
            {"recency_weight": -0.1},
            {"max_per_story": 0},
            {"simhash_bits": 65},
            {"simhash_bits": -1},
        ],
    )
    def test_malformed_parameters_are_refused(self, bad: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            params(**bad)

    def test_the_shipped_configuration_is_valid(self) -> None:
        """`configs/base.yaml` must construct -- including the recency bound, which
        the config model cannot check because it spans three settings."""
        built = fusion_params(Settings())
        assert built.pool == Settings().retrieval.max_k
        assert built.recency_weight < recency_weight_bound(
            rrf_k=built.rrf_k, pool=built.pool, relevance_weight=1.0
        )


# ---------------------------------------------------------------------------
# Diversity
# ---------------------------------------------------------------------------

WIRE = simhash(
    "The central bank held its benchmark interest rate steady on Thursday and signalled "
    "that further tightening remains possible if inflation proves persistent this year"
)


def near(fingerprint: int, bits: int) -> int:
    """``fingerprint`` with its lowest ``bits`` bits flipped."""
    return fingerprint ^ ((1 << bits) - 1)


class TestDiversity:
    def test_near_copies_of_one_passage_do_not_fill_the_slots(self) -> None:
        """Six mastheads, one wire story, six distinct stories behind them.

        Without the pass the top six are the six copies. With it, one copy is
        kept and the five slots go to five other stories.
        """
        copies = [
            candidate(
                f"wire-{i}",
                vector_rank=i + 1,
                document_id=f"masthead-{i}",
                simhash_value=near(WIRE, i),
            )
            for i in range(6)
        ]
        others = [
            candidate(
                f"other-{i}",
                vector_rank=i + 7,
                document_id=f"other-doc-{i}",
                simhash_value=simhash(
                    f"an entirely different article number {i} about topic {i * 7}"
                ),
            )
            for i in range(6)
        ]
        chosen = select(copies + others, k=6, params=params(recency_weight=0.0))
        assert order(chosen) == ["wire-0", "other-0", "other-1", "other-2", "other-3", "other-4"]
        assert len({entry.candidate.document_id for entry in chosen}) == 6

    def test_a_second_passage_of_the_same_story_is_allowed_up_to_the_cap(self) -> None:
        """New text from a represented story is evidence; the same text again is not."""
        rows = [
            candidate("s-0", vector_rank=1, document_id="story", ordinal=0, simhash_value=WIRE),
            candidate("s-1", vector_rank=2, document_id="story", ordinal=1, simhash_value=WIRE),
            candidate("s-2", vector_rank=3, document_id="story", ordinal=2, simhash_value=WIRE),
            candidate("x", vector_rank=4, document_id="x", simhash_value=simhash("unrelated text")),
        ]
        chosen = order(select(rows, k=3, params=params(recency_weight=0.0, max_per_story=2)))
        assert chosen == ["s-0", "s-1", "x"]

    def test_distinct_documents_are_all_kept(self) -> None:
        """The pass must cost nothing when there is nothing to suppress."""
        rows = [
            candidate(
                f"c{i}",
                vector_rank=i + 1,
                document_id=f"d{i}",
                simhash_value=simhash(f"article {i} concerns subject number {i * 13} entirely"),
            )
            for i in range(10)
        ]
        assert order(select(rows, k=6, params=params(recency_weight=0.0))) == [
            f"c{i}" for i in range(6)
        ]

    def test_k_is_still_filled_when_only_copies_remain(self) -> None:
        """Suppression reorders who gets a slot; it never hands back fewer chunks
        than exist. Overflow (new text) backfills before copies (the same text)."""
        rows = [
            candidate("a-0", vector_rank=1, document_id="a", ordinal=0, simhash_value=WIRE),
            candidate(
                "copy", vector_rank=2, document_id="b", ordinal=0, simhash_value=near(WIRE, 2)
            ),
            candidate("a-1", vector_rank=3, document_id="a", ordinal=1, simhash_value=WIRE),
            candidate("a-2", vector_rank=4, document_id="a", ordinal=2, simhash_value=WIRE),
        ]
        chosen = order(select(rows, k=4, params=params(recency_weight=0.0, max_per_story=2)))
        assert chosen == ["a-0", "a-1", "a-2", "copy"]

    def test_fewer_candidates_than_k_returns_them_all(self) -> None:
        rows = [candidate("a", vector_rank=1), candidate("b", keyword_rank=1)]
        assert len(select(rows, k=6, params=params())) == 2

    def test_a_zero_fingerprint_is_not_a_fingerprint(self) -> None:
        """`simhash` of a text with no shingles is 0. Two such documents share
        nothing but having nothing to hash, and must not be read as copies."""
        rows = [
            candidate("a", vector_rank=1, document_id="a", simhash_value=0),
            candidate("b", vector_rank=2, document_id="b", simhash_value=0),
            candidate("c", vector_rank=3, document_id="c", simhash_value=0),
            candidate("d", vector_rank=4, document_id="d", simhash_value=WIRE),
        ]
        # One chunk per story: were the three zeros one story, `d` would take
        # the second slot ahead of `b`.
        one_each = params(recency_weight=0.0, max_per_story=1)
        assert order(select(rows, k=2, params=one_each)) == ["a", "b"]
        fused = fuse(rows, one_each)
        assert order(diversify(fused, k=3, params=one_each)) == ["a", "b", "c"]

    def test_stories_do_not_grow_by_chaining(self) -> None:
        """A is 6 bits from B and B is 6 from C, but A is 12 from C. C is its own
        story: membership is measured from the story's founder, not its members."""
        a, b, c = WIRE, near(WIRE, 6), near(WIRE, 6) ^ (((1 << 6) - 1) << 20)
        assert hamming64(a, b) == 6 and hamming64(b, c) == 6 and hamming64(a, c) == 12
        rows = [
            candidate("a", vector_rank=1, document_id="a", ordinal=0, simhash_value=a),
            candidate("b", vector_rank=2, document_id="b", ordinal=0, simhash_value=b),
            candidate("c", vector_rank=3, document_id="c", ordinal=0, simhash_value=c),
        ]
        chosen = order(select(rows, k=2, params=params(recency_weight=0.0, simhash_bits=8)))
        assert chosen == ["a", "c"]

    def test_the_threshold_is_inclusive_and_configurable(self) -> None:
        rows = [
            candidate("a", vector_rank=1, document_id="a", simhash_value=WIRE),
            candidate("b", vector_rank=2, document_id="b", simhash_value=near(WIRE, 8)),
            candidate("c", vector_rank=3, document_id="c", simhash_value=simhash("other words")),
        ]
        assert order(select(rows, k=2, params=params(recency_weight=0.0, simhash_bits=8))) == [
            "a",
            "c",
        ]
        assert order(select(rows, k=2, params=params(recency_weight=0.0, simhash_bits=7))) == [
            "a",
            "b",
        ]

    def test_k_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="k must be positive"):
            diversify((), k=0, params=params())


class TestHamming:
    def test_it_agrees_with_the_corpus_definition(self) -> None:
        """Restated in `fusion` because the corpus package is off the pure list;
        this is what keeps the restatement honest."""
        left, right = simhash("one article about a merger"), simhash("another about a strike")
        assert hamming64(left, right) == hamming(left, right)

    def test_a_signed_fingerprint_measures_the_same_distance(self) -> None:
        """Postgres hands the SimHash back as a signed bigint."""
        left = (1 << 63) | 0b1011  # negative once signed
        right = 0b0001
        assert to_signed(left) < 0
        assert hamming64(to_signed(left), to_signed(right)) == hamming(left, right) == 3
        assert from_signed(to_signed(left)) == left

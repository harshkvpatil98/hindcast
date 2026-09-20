"""Fusion and diversity for hybrid retrieval (migration 018).

Pure: no I/O, no clock, no RNG. ``chronofence_search_hybrid`` supplies a
time-locked candidate union; everything that *ranks* it lives here, because it
is arithmetic over at most 400 rows and a pure function can be property-tested
where a SQL function cannot.

**Nothing in this module can widen the time lock.** It reorders and truncates
the candidates it is handed and cannot produce a chunk it was not given, so a
candidate set that respects ``as_of`` yields a result that does.

**Reciprocal-rank fusion.** Three rankings of the same union are combined:

* the vector pool's rank (embedding distance),
* the keyword pool's rank (entity coverage, then distance), and
* recency -- newer ``published_at`` first.

``score = sum_i  w_i / (rrf_k + rank_i)``, a term being absent where a
candidate is absent from that list. RRF is used because it needs only ranks:
an L2 distance and an entity-coverage count share no scale, and any weighted
sum of raw scores would be a calibration nobody here can measure. There is no
labelled relevance set to fit weights against, and fitting them against
forecast outcomes is what the measurement contract forbids.

**Recency is a ranking, never a filter.** Every candidate is in the recency
list, so recency can reorder the union and cannot remove anything from it: the
fused ranking is always a permutation of its input. How far it may reorder is
bounded by construction (:func:`recency_weight_bound`) -- the newest candidate
at the *bottom* of a relevance list must not outrank the oldest at the *top*
of one. Old background that is strongly relevant still wins; recency breaks
near-ties among comparably relevant chunks, which is its whole job.

Recency ranks are *competition* ranks over exact timestamps: chunks of one
document share a ``published_at`` and therefore a rank. Ordering them by
``chunk_id`` inside the recency list would hand one chunk of an article a
better score than its sibling for no reason but its id.

**Diversity keys on the document fingerprint, not on embeddings.** The
alternative is maximal marginal relevance over chunk vectors, which would mean
returning ~300 kB of ``halfvec`` per query and adding a relevance/novelty
trade-off parameter with nothing to set it against. The failure being guarded
is narrower than "similar chunks": it is one *story* occupying every slot --
several chunks of one article, or the same wire copy under several mastheads.
The corpus already fingerprints every document with a 64-bit SimHash at ingest
for exactly that second case (spec §3.2), it rides along on a join the query
already pays for, and Hamming distance over it has no free parameter beyond a
bit threshold with a closed-form false-positive rate. MMR's cosine over a
small embedder would also inherit the weakness this whole path exists to
route around: it cannot tell two companies' stories apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cascade.retrieval.schema import HybridCandidate

__all__ = [
    "FusedCandidate",
    "FusionParams",
    "diversify",
    "fuse",
    "hamming64",
    "recency_ranks",
    "recency_weight_bound",
    "select",
]

_MASK64 = (1 << 64) - 1


def recency_weight_bound(*, rrf_k: int, pool: int, relevance_weight: float) -> float:
    """The recency weight at which recency starts overruling relevance.

    Take the oldest candidate, first in one relevance list, against the newest
    candidate, last (``pool``-th) in one. The first must win:

        w_rel/(k+1) + w_rec/(k+N)  >  w_rel/(k+pool) + w_rec/(k+1)

    for every union size N. The left-hand recency term only helps, so dropping
    it gives the bound that holds for all N:

        w_rec  <  w_rel * (1 - (k+1)/(k+pool))

    At ``rrf_k`` 60 and a pool of 200 that is 0.765 of the relevance weight.
    Below it, recency can only ever break ties among candidates whose
    relevance ranks are themselves close.
    """
    if rrf_k <= 0 or pool <= 1:
        raise ValueError(f"rrf_k must be positive and pool above 1, got {rrf_k} and {pool}")
    return relevance_weight * (1.0 - (rrf_k + 1) / (rrf_k + pool))


@dataclass(frozen=True)
class FusionParams:
    """Everything that shapes a fused ranking, validated once.

    Refuses a recency weight at or above :func:`recency_weight_bound`. The
    check lives here rather than in the config model because the bound is a
    function of three settings together, and a number that can be raised past
    it in a YAML file is not a bound.
    """

    rrf_k: int
    vector_weight: float
    keyword_weight: float
    recency_weight: float
    pool: int
    max_per_story: int
    simhash_bits: int

    def __post_init__(self) -> None:
        if self.rrf_k <= 0:
            raise ValueError(f"rrf_k must be positive, got {self.rrf_k}")
        if self.vector_weight <= 0.0 or self.keyword_weight <= 0.0:
            raise ValueError(
                "relevance weights must be positive, got "
                f"vector={self.vector_weight}, keyword={self.keyword_weight}"
            )
        if self.recency_weight < 0.0:
            raise ValueError(f"recency_weight must not be negative, got {self.recency_weight}")
        if self.max_per_story <= 0:
            raise ValueError(f"max_per_story must be positive, got {self.max_per_story}")
        if not 0 <= self.simhash_bits <= 64:
            raise ValueError(f"simhash_bits must lie in [0, 64], got {self.simhash_bits}")
        bound = recency_weight_bound(
            rrf_k=self.rrf_k,
            pool=self.pool,
            relevance_weight=min(self.vector_weight, self.keyword_weight),
        )
        if self.recency_weight >= bound:
            raise ValueError(
                f"recency_weight {self.recency_weight} is not below {bound:.4f}, the value at "
                f"which the newest candidate at the bottom of a {self.pool}-row relevance "
                "list outranks the oldest at the top of one; recency must break ties, not "
                "overrule relevance"
            )


@dataclass(frozen=True)
class FusedCandidate:
    """A candidate with the three ranks it was scored on."""

    candidate: HybridCandidate
    recency_rank: int
    score: float


def recency_ranks(candidates: Sequence[HybridCandidate]) -> dict[str, int]:
    """Competition rank by ``published_at``, newest first, keyed by chunk id.

    Equal timestamps share a rank (1, 1, 3, ...), so the rank is a function of
    the timestamp alone -- never of input order or of an id.
    """
    newest_first = sorted({item.published_at for item in candidates}, reverse=True)
    counts: dict[object, int] = {}
    for item in candidates:
        counts[item.published_at] = counts.get(item.published_at, 0) + 1
    rank_of: dict[object, int] = {}
    ahead = 0
    for stamp in newest_first:
        rank_of[stamp] = ahead + 1
        ahead += counts[stamp]
    return {item.chunk_id: rank_of[item.published_at] for item in candidates}


def fuse(candidates: Sequence[HybridCandidate], params: FusionParams) -> tuple[FusedCandidate, ...]:
    """Rank the whole union by reciprocal-rank fusion. Best first.

    Preserves three invariants. The output is a **permutation** of the input:
    fusion orders, it never drops, so recency cannot act as a filter. The
    output is a function of the input **set**: a candidate's score is computed
    from its own ranks in a fixed term order, and ties fall to ``chunk_id``, so
    the order rows arrived in cannot reach the result (invariant 7, and the
    replay hash downstream). And a candidate present in only **one** relevance
    list still scores -- absence contributes nothing rather than disqualifying.
    """
    seen: set[str] = set()
    for item in candidates:
        if item.chunk_id in seen:
            raise ValueError(
                f"chunk {item.chunk_id!r} appears twice in the candidate union; ranks over "
                "a multiset are not well defined"
            )
        seen.add(item.chunk_id)

    recency = recency_ranks(candidates)
    fused: list[FusedCandidate] = []
    for item in candidates:
        # Summed in a fixed order -- vector, keyword, recency -- because float
        # addition is not associative and the score is a sort key.
        score = 0.0
        if item.vector_rank is not None:
            score += params.vector_weight / (params.rrf_k + item.vector_rank)
        if item.keyword_rank is not None:
            score += params.keyword_weight / (params.rrf_k + item.keyword_rank)
        score += params.recency_weight / (params.rrf_k + recency[item.chunk_id])
        fused.append(
            FusedCandidate(candidate=item, recency_rank=recency[item.chunk_id], score=score)
        )
    return tuple(sorted(fused, key=lambda entry: (-entry.score, entry.candidate.chunk_id)))


def hamming64(left: int, right: int) -> int:
    """Differing bits between two 64-bit fingerprints, signed or unsigned.

    Postgres stores the SimHash as a signed ``bigint``; masking the XOR
    compares bit patterns, so a fingerprint read back negative measures the
    same distance as its unsigned original. Restated here rather than imported
    from ``cascade.corpus.simhash`` because this module is on the pure list
    and the corpus package is not; a unit test asserts the two agree.
    """
    return ((left ^ right) & _MASK64).bit_count()


@dataclass
class _Story:
    """One article and its near-copies, as the selection has seen them so far."""

    fingerprint: int
    documents: set[str]
    passages: set[tuple[str, int]]
    taken: int


def _story_of(
    item: HybridCandidate, stories: Sequence[_Story], *, simhash_bits: int
) -> _Story | None:
    """The first story ``item`` belongs to, in the order stories were opened.

    Compared against each story's *founding* fingerprint only. Matching against
    every member would chain -- A near B, B near C, so C joins A's story at
    twice the threshold -- and a story would grow by drift.

    A zero fingerprint is "no fingerprint" (``simhash`` of a text with no
    shingles), not a fingerprint that happens to be zero: two such documents
    have nothing in common but having nothing to hash.
    """
    for story in stories:
        if item.document_id in story.documents:
            return story
        if (
            item.simhash != 0
            and story.fingerprint != 0
            and hamming64(item.simhash, story.fingerprint) <= simhash_bits
        ):
            return story
    return None


def diversify(
    fused: Sequence[FusedCandidate], *, k: int, params: FusionParams
) -> tuple[FusedCandidate, ...]:
    """Take ``k`` from a fused ranking without letting one story take them all.

    Walks best-first. A candidate opens a new story, joins one that still has
    room (``max_per_story``), or is set aside. Two kinds of set-aside, kept
    apart because they are worth different amounts:

    * an **overflow** chunk is new text from a story already represented;
    * a **copy** is the same passage again -- same ordinal, sibling document --
      which is what syndication produces and carries no new evidence at all.

    Preserves the guarantee that ``k`` is filled whenever the union holds ``k``
    candidates: set-asides backfill, overflow before copies, each in fused
    order. Suppression reorders who gets a slot; it never returns fewer chunks
    than the caller could have had, because a short evidence block also costs
    the agent prefix its cache floor (ADR-0019).

    Backfilled chunks follow the diverse ones rather than being merged back
    into score order, so the head of the result is always the diverse set.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")

    stories: list[_Story] = []
    chosen: list[FusedCandidate] = []
    overflow: list[FusedCandidate] = []
    copies: list[FusedCandidate] = []

    for entry in fused:
        if len(chosen) == k:
            break
        item = entry.candidate
        story = _story_of(item, stories, simhash_bits=params.simhash_bits)
        if story is None:
            stories.append(
                _Story(
                    fingerprint=item.simhash,
                    documents={item.document_id},
                    passages={(item.document_id, item.ordinal)},
                    taken=1,
                )
            )
            chosen.append(entry)
            continue

        is_copy = any(
            ordinal == item.ordinal and document_id != item.document_id
            for document_id, ordinal in sorted(story.passages)
        )
        if is_copy:
            copies.append(entry)
        elif story.taken >= params.max_per_story:
            overflow.append(entry)
        else:
            story.documents.add(item.document_id)
            story.passages.add((item.document_id, item.ordinal))
            story.taken += 1
            chosen.append(entry)

    for entry in (*overflow, *copies):
        if len(chosen) == k:
            break
        chosen.append(entry)
    return tuple(chosen)


def select(
    candidates: Sequence[HybridCandidate], *, k: int, params: FusionParams
) -> tuple[FusedCandidate, ...]:
    """Fuse, then diversify: the ``k`` chunks the hybrid path returns."""
    return diversify(fuse(candidates, params), k=k, params=params)

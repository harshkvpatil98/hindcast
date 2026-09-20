"""Reranking: a permutation of an already time-locked pool (ADR-0047).

Pure: no I/O, no clock, no RNG. Everything that *reaches* a reranking service
lives behind :class:`Reranker`, and the only implementation in this module
scores locally.

**The interface makes a time-lock violation unrepresentable, not merely
forbidden.** A reranker is handed a query string and document *bodies*, and
returns one score per document, positionally. It never sees a ``chunk_id``, a
``published_at`` or ``as_of``; there is no argument through which it could be
told one and no return value through which it could name a document. The
permutation is applied here, by the caller, over the candidates it already
held. So a reranker that is buggy, adversarial or simply a different model
than the one recorded can mis-order the evidence -- which is a quality
failure, measurable against the exact oracle -- and cannot introduce a chunk
the database did not return, which would be a validity failure.

That is the whole argument of ADR-0047 expressed as a type signature, and it
is why a managed service is admissible here and not in ADR-0030's filter.

**Determinism.** The fused ranking that reaches this module is already a
deterministic function of the corpus and the query, and M8's criterion is a
byte-identical event-log hash across processes. So: scores are summed with
``math.fsum`` (float addition is not associative, and this project has been
bitten by that three times), the final ordering breaks ties on ``chunk_id``
after the score, and a non-finite score is an error rather than a value to
sort. A remote reranker is additionally cached by
:func:`rerank_cache_key`, so a provider that updates a model changes the key
rather than silently reordering evidence beneath a stored graph.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from cascade.retrieval.schema import RetrievedChunk

__all__ = [
    "BM25_B",
    "BM25_K1",
    "LexicalReranker",
    "RerankError",
    "Reranker",
    "apply_scores",
    "rerank_cache_key",
    "tokenize",
]

# Robertson's defaults. Not tuned here: there is no labelled relevance set to
# tune against, and tuning them against forecast outcomes is what §1 forbids.
BM25_K1 = 1.2
BM25_B = 0.75

_WORD = re.compile(r"[a-z0-9]+")


class RerankError(RuntimeError):
    """A reranker returned something that cannot be a permutation.

    Distinct from an unreachable service: a wrong *number* of scores, or a
    score that is not a finite number, means the response does not describe
    the pool it was given, and applying it would silently drop or duplicate
    evidence.
    """


class Reranker(Protocol):
    """Scores documents against a query, by position.

    Deliberately narrow. Implementations receive text and return numbers; they
    are given no identifier they could return and no date they could compare.
    """

    @property
    def model_id(self) -> str:
        """Identifies the scoring function in the cache key and the event log."""
        ...

    def score(self, *, query: str, documents: Sequence[str]) -> Sequence[float]:
        """One relevance score per document, higher is better, same order."""
        ...


def tokenize(text: str) -> tuple[str, ...]:
    """Lowercase alphanumeric runs, in order of appearance.

    Preserves the only property BM25 needs of a tokenizer: that the same
    string always yields the same terms. Deliberately not the corpus chunker's
    tokenizer, which is the embedder's and is a model artifact.
    """
    return tuple(_WORD.findall(text.lower()))


def apply_scores(
    chunks: Sequence[RetrievedChunk],
    scores: Sequence[float],
    *,
    top_k: int,
) -> tuple[RetrievedChunk, ...]:
    """Permute ``chunks`` by ``scores`` and truncate to ``top_k``.

    The single place a rerank result becomes an ordering, so the subset
    property holds by construction for every reranker: the output is built
    from ``chunks`` by index and nothing else is reachable from here. The
    returned rows are the same rows, carrying the rank and score the reranker
    gave them beside the ranks it was given -- so the displacement is in the
    result rather than inferable only by running the query twice.

    Raises :class:`RerankError` when the scores do not describe the pool.
    """
    if len(scores) != len(chunks):
        raise RerankError(
            f"reranker returned {len(scores)} score(s) for {len(chunks)} candidate(s); "
            "a response that does not describe the pool cannot be applied to it"
        )
    for position, value in enumerate(scores):
        if not math.isfinite(value):
            raise RerankError(
                f"reranker returned a non-finite score {value!r} at position {position}; "
                "a score that cannot be ordered is an error, not a ranking"
            )
    if top_k < 0:
        raise ValueError(f"top_k must not be negative, got {top_k}")

    # Sorted by score descending, then chunk_id -- the same tie-break the SQL
    # and the fusion use, so two equally scored chunks cannot swap between
    # replays (M8).
    order = sorted(
        range(len(chunks)),
        key=lambda i: (-scores[i], chunks[i].chunk_id),
    )
    return tuple(
        chunks[index].model_copy(
            update={"rerank_rank": position + 1, "rerank_score": scores[index]}
        )
        for position, index in enumerate(order[:top_k])
    )


def rerank_cache_key(
    *,
    model_id: str,
    query: str,
    chunk_ids: Sequence[str],
    top_k: int,
) -> str:
    """Content-address one rerank call (ADR-0047).

    Keyed on the *pool's identity* rather than its text: the bodies are a pure
    function of the chunk ids, and hashing megabytes of prose to look up a
    ranking of twenty rows would make the cache more expensive than the call.
    The ids are hashed in the order they will be sent, because a reranker is
    permitted to be position-sensitive and two orders are two calls.

    ``model_id`` is in the key so changing reranker invalidates recordings
    instead of reordering evidence under a graph that was compiled against the
    old ordering.
    """
    digest = hashlib.blake2b(digest_size=16)
    for part in (model_id, str(top_k), query):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    for chunk_id in chunk_ids:
        digest.update(chunk_id.encode("utf-8"))
        digest.update(b"\x00")
    return f"rerank-{digest.hexdigest()}"


@dataclass(frozen=True)
class LexicalReranker:
    """BM25 over the pool, as the default that needs no credential.

    The pool is its own corpus for the purpose of inverse document frequency,
    which is the honest reading at this size: twenty to two hundred rows that
    a first-stage retriever already judged relevant, where the discriminating
    term is the one *most* of them lack.

    **This is a default, not a claim.** It is here so the seam has a
    deterministic implementation, so ``make demo`` runs with no AWS account,
    and so a rerank-vs-no-rerank comparison has a third arm that is neither a
    network call nor a no-op. Whether it beats the fused ordering is measured
    on the dev split like anything else, and it is entirely permitted to lose.
    """

    model_id: str = "bm25-local-v1"

    def score(self, *, query: str, documents: Sequence[str]) -> Sequence[float]:
        """BM25 relevance of each document to ``query``, in the given order."""
        terms = tokenize(query)
        if not terms or not documents:
            # No signal: every document scores zero, so `apply_scores` falls
            # through to the chunk_id tie-break and the fused order is
            # preserved for the pool it was given. A reranker with nothing to
            # say must not silently reshuffle.
            return [0.0] * len(documents)

        tokenized = [tokenize(body) for body in documents]
        lengths = [len(doc) for doc in tokenized]
        total = math.fsum(float(length) for length in lengths)
        avg_length = total / len(tokenized) if tokenized else 0.0

        counts: list[dict[str, int]] = []
        for doc in tokenized:
            table: dict[str, int] = {}
            for token in doc:
                table[token] = table.get(token, 0) + 1
            counts.append(table)

        unique_terms = sorted(set(terms))
        idf: dict[str, float] = {}
        n_docs = len(tokenized)
        for term in unique_terms:
            containing = sum(1 for table in counts if term in table)
            # Robertson/Sparck Jones with the +1 that keeps it positive: a term
            # present in every candidate scores ~0 rather than negative, so a
            # ubiquitous word cannot penalise the document that repeats it.
            idf[term] = math.log(1.0 + (n_docs - containing + 0.5) / (containing + 0.5))

        scores: list[float] = []
        for table, length in zip(counts, lengths, strict=True):
            contributions = []
            for term in unique_terms:
                frequency = float(table.get(term, 0))
                if frequency == 0.0:
                    continue
                denominator = frequency + BM25_K1 * (
                    1.0 - BM25_B + BM25_B * (length / avg_length if avg_length else 0.0)
                )
                contributions.append(idf[term] * (frequency * (BM25_K1 + 1.0)) / denominator)
            # fsum over a sorted-term order: the same document must score the
            # same bits however the pool was ordered.
            scores.append(math.fsum(contributions))
        return scores

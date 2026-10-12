"""The poison-pill probe run *through* the rerank path (ADR-0047, M15).

ADR-0047 claims a managed reranker is admissible where a managed knowledge
base was not, because a reranker permutes a set the database already filtered
rather than replacing the filter. A claim about a security boundary that is
only argued in prose is not a boundary, so it is measured here, against the
real corpus, with a reranker that is *actively hunting for the poison*.

The adversarial reranker scores any document carrying the poison's own
signature phrase above everything else. If a single post-resolution document
could reach the pool, it would come back ranked first -- the most visible
possible failure. The criterion is unchanged from M3: 0 retrieved.

This is the sharper form of the test. The ordinary poison-pill probes ask
whether the retriever leaks; this one grants an attacker control of the
ranking stage and asks whether that is enough. It should not be, because the
ranking stage is handed bodies and returns numbers, and has no way to name a
document the SQL did not already return.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
import pytest

from cascade.config import Settings
from cascade.retrieval.leakage import (
    POISON_MARKER,
    build_poison_documents,
    insert_poison,
    violations,
)
from cascade.retrieval.search import Chronofence

pytestmark = pytest.mark.leakage

# Only the synthetic poison carries this; `build_poison_documents` writes
# "Final determination: ..." into every body it makes.
POISON_SIGNATURE = "final determination"


class PoisonHuntingReranker:
    """An adversarial reranker: promote the poison, whatever else is there.

    Deliberately the worst reranker that could be substituted for a legitimate
    one -- a compromised managed endpoint, a model fine-tuned by an attacker,
    a bug that happens to favour post-resolution text. It still only receives
    bodies and returns floats, which is the whole of ADR-0047's argument.
    """

    model_id = "poison-hunter-v1"

    def score(self, *, query: str, documents: Sequence[str]) -> Sequence[float]:
        return [
            1_000.0 if POISON_SIGNATURE in body.lower() else float(-index)
            for index, body in enumerate(documents)
        ]


def rerank_enabled(settings: Settings) -> Settings:
    """A copy of ``settings`` with reranking on, leaving the original alone."""
    retrieval = settings.retrieval.model_copy(
        update={"rerank": settings.retrieval.rerank.model_copy(update={"enabled": True})}
    )
    return settings.model_copy(update={"retrieval": retrieval})


@pytest.fixture
def poisoned(live_settings: Settings, records: tuple[Any, ...], embedder: Any) -> Any:
    """Insert the poison, yield an open transaction, then roll it back."""
    count = live_settings.retrieval.poison_pill_count
    documents = build_poison_documents(records, count=count)
    vectors = embedder.encode([document.body for document in documents])

    conn = psycopg.connect(live_settings.database_url("admin"), connect_timeout=30)
    conn.autocommit = False
    try:
        written = insert_poison(conn, documents, vectors)
        assert written == count
        yield conn, documents
    finally:
        # Never commit. This is the line that keeps the probe from becoming
        # the leak it is testing for.
        conn.rollback()
        conn.close()


def test_the_hunting_reranker_would_promote_poison_if_it_could_see_it() -> None:
    """The positive control.

    Without this, a reranker that scored everything zero would pass the probe
    below while proving nothing -- the same reason the M3 probe asserts the
    poison *is* retrievable once the time filter is removed.
    """
    reranker = PoisonHuntingReranker()
    scores = reranker.score(
        query="anything",
        documents=["an ordinary news chunk", "... Final determination: the question resolved YES."],
    )
    assert scores[1] > scores[0]
    assert scores[1] == 1_000.0


def test_an_adversarial_reranker_cannot_surface_poison(
    poisoned: Any, live_settings: Settings, embedder: Any
) -> None:
    """0 poison documents retrieved, with the ranking stage compromised."""
    _conn, documents = poisoned
    by_scenario: dict[str, Any] = {}
    for document in documents:
        by_scenario.setdefault(document.scenario_id, document)

    settings = rerank_enabled(live_settings)
    vectors = embedder.encode([document.body for document in by_scenario.values()])
    leaked: list[str] = []

    with Chronofence(settings, role="eval", reranker=PoisonHuntingReranker()) as fence:
        for (scenario_id, document), vector in zip(by_scenario.items(), vectors, strict=True):
            result = fence.retrieve(
                vector,
                text=document.body,
                as_of=document.cutoff_ts,
                k=20,
            )
            for chunk in result.chunks:
                if POISON_MARKER in chunk.document_id:
                    leaked.append(f"{scenario_id}:{chunk.chunk_id}")

    assert leaked == [], (
        f"{len(leaked)} poison document(s) reached a caller through the rerank path, "
        f"which would mean the reranker can name a chunk the SQL did not return: {leaked[:5]}"
    )


def test_no_reranked_result_postdates_its_cutoff(
    poisoned: Any, live_settings: Settings, embedder: Any
) -> None:
    """The broader property: nothing at or after ``as_of``, poison or not.

    Re-asserted on the caller's side rather than trusted, exactly as the
    unreranked probes do -- a reranker that edited `published_at` on the rows
    it returned would be caught here.
    """
    _conn, documents = poisoned
    by_scenario: dict[str, Any] = {}
    for document in documents:
        by_scenario.setdefault(document.scenario_id, document)

    settings = rerank_enabled(live_settings)
    vectors = embedder.encode([document.body for document in by_scenario.values()])
    offenders: list[str] = []

    with Chronofence(settings, role="eval", reranker=PoisonHuntingReranker()) as fence:
        for (scenario_id, document), vector in zip(by_scenario.items(), vectors, strict=True):
            result = fence.retrieve(vector, text=document.body, as_of=document.cutoff_ts, k=20)
            for bad in violations(result.chunks, as_of=document.cutoff_ts):
                offenders.append(f"{scenario_id}:{bad.chunk_id}@{bad.published_at}")

    assert offenders == [], f"reranked chunks at or after their cutoff: {offenders[:5]}"


def test_the_reranked_result_is_a_subset_of_the_pool_it_was_given(
    live_settings: Settings, records: tuple[Any, ...], embedder: Any
) -> None:
    """ADR-0047's first claim, measured on the real corpus.

    Runs with no poison inserted: this is about the permutation property
    itself, over the population the study actually retrieves from.
    """
    sample = records[:20]
    settings = rerank_enabled(live_settings)
    vectors = embedder.encode([record.scenario.question for record in sample])
    pool_size = settings.retrieval.rerank.pool

    with (
        Chronofence(settings, role="eval", reranker=PoisonHuntingReranker()) as plain,
        Chronofence(settings, role="eval", reranker=PoisonHuntingReranker()) as fence,
    ):
        for record, vector in zip(sample, vectors, strict=True):
            cutoff = record.scenario.cutoff_ts
            question = record.scenario.question
            # The pool the reranker was shown, fetched the way `retrieve`
            # fetches it. Reaching past the public surface is the point here:
            # the claim is about what the reranker was *given*.
            pool = plain._retrieve_pool(
                vector, text=question, entities=(), as_of=cutoff, k=pool_size
            )
            reranked = fence.retrieve(vector, text=question, as_of=cutoff, k=20)

            assert set(reranked.chunk_ids) <= set(pool.chunk_ids), (
                f"{record.scenario.scenario_id}: reranking returned chunk(s) that were "
                "not in the pool it was handed"
            )
            assert reranked.rerank_model == "poison-hunter-v1"
            assert len(reranked.chunks) <= 20
